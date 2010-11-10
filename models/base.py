from django import forms
from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.utils import simplejson as json
from django.core.exceptions import ObjectDoesNotExist
from philo.exceptions import AncestorDoesNotExist
from philo.models.fields import JSONField
from philo.utils import ContentTypeRegistryLimiter, ContentTypeSubclassLimiter
from philo.signals import entity_class_prepared
from philo.validators import json_validator
from UserDict import DictMixin


class Tag(models.Model):
	name = models.CharField(max_length=255)
	slug = models.SlugField(max_length=255, unique=True)
	
	def __unicode__(self):
		return self.name
	
	class Meta:
		app_label = 'philo'


class Titled(models.Model):
	title = models.CharField(max_length=255)
	slug = models.SlugField(max_length=255)
	
	def __unicode__(self):
		return self.title
	
	class Meta:
		abstract = True


value_content_type_limiter = ContentTypeRegistryLimiter()


def register_value_model(model):
	value_content_type_limiter.register_class(model)


def unregister_value_model(model):
	value_content_type_limiter.unregister_class(model)


class AttributeValue(models.Model):
	attribute_set = generic.GenericRelation('Attribute', content_type_field='value_content_type', object_id_field='value_object_id')
	
	@property
	def attribute(self):
		return self.attribute_set.all()[0]
	
	def apply_data(self, data):
		raise NotImplementedError
	
	def value_formfield(self, **kwargs):
		raise NotImplementedError
	
	def __unicode__(self):
		return unicode(self.value)
	
	class Meta:
		abstract = True


attribute_value_limiter = ContentTypeSubclassLimiter(AttributeValue)


class JSONValue(AttributeValue):
	value = JSONField() #verbose_name='Value (JSON)', help_text='This value must be valid JSON.')
	
	def __unicode__(self):
		return self.value_json
	
	def value_formfield(self, **kwargs):
		kwargs['initial'] = self.value_json
		return self._meta.get_field('value').formfield(**kwargs)
	
	def apply_data(self, cleaned_data):
		self.value = cleaned_data.get('value', None)
	
	class Meta:
		app_label = 'philo'


class ForeignKeyValue(AttributeValue):
	content_type = models.ForeignKey(ContentType, limit_choices_to=value_content_type_limiter, verbose_name='Value type', null=True, blank=True)
	object_id = models.PositiveIntegerField(verbose_name='Value ID', null=True, blank=True)
	value = generic.GenericForeignKey()
	
	def value_formfield(self, form_class=forms.ModelChoiceField, **kwargs):
		if self.content_type is None:
			return None
		kwargs.update({'initial': self.object_id, 'required': False})
		return form_class(self.content_type.model_class()._default_manager.all(), **kwargs)
	
	def apply_data(self, cleaned_data):
		if 'value' in cleaned_data and cleaned_data['value'] is not None:
			self.value = cleaned_data['value']
		else:
			self.content_type = cleaned_data.get('content_type', None)
			# If there is no value set in the cleaned data, clear the stored value.
			self.object_id = None
	
	class Meta:
		app_label = 'philo'


class ManyToManyValue(AttributeValue):
	content_type = models.ForeignKey(ContentType, limit_choices_to=value_content_type_limiter, verbose_name='Value type', null=True, blank=True)
	values = models.ManyToManyField(ForeignKeyValue, blank=True, null=True)
	
	def get_object_id_list(self):
		if not self.values.count():
			return []
		else:
			return self.values.values_list('object_id', flat=True)
	
	def get_value(self):
		if self.content_type is None:
			return None
		
		return self.content_type.model_class()._default_manager.filter(id__in=self.get_object_id_list())
	
	def set_value(self, value):
		# Value is probably a queryset - but allow any iterable.
		
		# These lines shouldn't be necessary; however, if value is an EmptyQuerySet,
		# the code (specifically the object_id__in query) won't work without them. Unclear why...
		if not value:
			value = []
		
		# Before we can fiddle with the many-to-many to foreignkeyvalues, we need
		# a pk.
		if self.pk is None:
			self.save()
		
		if isinstance(value, models.query.QuerySet):
			value = value.values_list('id', flat=True)
		
		self.values.filter(~models.Q(object_id__in=value)).delete()
		current = self.get_object_id_list()
		
		for v in value:
			if v in current:
				continue
			self.values.create(content_type=self.content_type, object_id=v)
	
	value = property(get_value, set_value)
	
	def value_formfield(self, form_class=forms.ModelMultipleChoiceField, **kwargs):
		if self.content_type is None:
			return None
		kwargs.update({'initial': self.get_object_id_list(), 'required': False})
		return form_class(self.content_type.model_class()._default_manager.all(), **kwargs)
	
	def apply_data(self, cleaned_data):
		if 'value' in cleaned_data and cleaned_data['value'] is not None:
			self.value = cleaned_data['value']
		else:
			self.content_type = cleaned_data.get('content_type', None)
			# If there is no value set in the cleaned data, clear the stored value.
			self.value = []
	
	class Meta:
		app_label = 'philo'


class Attribute(models.Model):
	entity_content_type = models.ForeignKey(ContentType, related_name='attribute_entity_set', verbose_name='Entity type')
	entity_object_id = models.PositiveIntegerField(verbose_name='Entity ID')
	entity = generic.GenericForeignKey('entity_content_type', 'entity_object_id')
	
	value_content_type = models.ForeignKey(ContentType, related_name='attribute_value_set', limit_choices_to=attribute_value_limiter, verbose_name='Value type', null=True, blank=True)
	value_object_id = models.PositiveIntegerField(verbose_name='Value ID', null=True, blank=True)
	value = generic.GenericForeignKey('value_content_type', 'value_object_id')
	
	key = models.CharField(max_length=255)
	
	def __unicode__(self):
		return u'"%s": %s' % (self.key, self.value)
	
	class Meta:
		app_label = 'philo'
		unique_together = (('key', 'entity_content_type', 'entity_object_id'), ('value_content_type', 'value_object_id'))


class QuerySetMapper(object, DictMixin):
	def __init__(self, queryset, passthrough=None):
		self.queryset = queryset
		self.passthrough = passthrough
	
	def __getitem__(self, key):
		try:
			value = self.queryset.get(key__exact=key).value
		except ObjectDoesNotExist:
			if self.passthrough is not None:
				return self.passthrough.__getitem__(key)
			raise KeyError
		else:
			if value is not None:
				return value.value
			return value
	
	def keys(self):
		keys = set(self.queryset.values_list('key', flat=True).distinct())
		if self.passthrough is not None:
			keys |= set(self.passthrough.keys())
		return list(keys)


class EntityOptions(object):
	def __init__(self, options):
		if options is not None:
			for key, value in options.__dict__.items():
				setattr(self, key, value)
		if not hasattr(self, 'proxy_fields'):
			self.proxy_fields = []
	
	def add_proxy_field(self, proxy_field):
		self.proxy_fields.append(proxy_field)


class EntityBase(models.base.ModelBase):
	def __new__(cls, name, bases, attrs):
		new = super(EntityBase, cls).__new__(cls, name, bases, attrs)
		entity_options = attrs.pop('EntityMeta', None)
		setattr(new, '_entity_meta', EntityOptions(entity_options))
		entity_class_prepared.send(sender=new)
		return new


class Entity(models.Model):
	__metaclass__ = EntityBase
	
	attribute_set = generic.GenericRelation(Attribute, content_type_field='entity_content_type', object_id_field='entity_object_id')
	
	@property
	def attributes(self):
		return QuerySetMapper(self.attribute_set.all())
	
	@property
	def _added_attribute_registry(self):
		if not hasattr(self, '_real_added_attribute_registry'):
			self._real_added_attribute_registry = {}
		return self._real_added_attribute_registry
	
	@property
	def _removed_attribute_registry(self):
		if not hasattr(self, '_real_removed_attribute_registry'):
			self._real_removed_attribute_registry = []
		return self._real_removed_attribute_registry
	
	def save(self, *args, **kwargs):
		super(Entity, self).save(*args, **kwargs)
		
		for key in self._removed_attribute_registry:
			self.attribute_set.filter(key__exact=key).delete()
		del self._removed_attribute_registry[:]
		
		for field, value in self._added_attribute_registry.items():
			try:
				attribute = self.attribute_set.get(key__exact=field.key)
			except Attribute.DoesNotExist:
				attribute = Attribute()
				attribute.entity = self
				attribute.key = field.key
			
			field.set_attribute_value(attribute, value)
			attribute.save()
		self._added_attribute_registry.clear()
	
	class Meta:
		abstract = True


class TreeManager(models.Manager):
	use_for_related_fields = True
	
	def roots(self):
		return self.filter(parent__isnull=True)
	
	def get_branch_pks(self, root, depth=5, inclusive=True):
		branch_pks = []
		parent_pks = [root.pk]
		
		if inclusive:
			branch_pks.append(root.pk)
		
		for i in xrange(depth):
			child_pks = list(self.filter(parent__pk__in=parent_pks).exclude(pk__in=branch_pks).values_list('pk', flat=True))
			if not child_pks:
				break
			
			branch_pks += child_pks
			parent_pks = child_pks
		
		return branch_pks
	
	def get_branch(self, root, depth=5, inclusive=True):
		return self.filter(pk__in=self.get_branch_pks(root, depth, inclusive))
	
	def get_with_path(self, path, root=None, absolute_result=True, pathsep='/', field='slug'):
		"""
		Returns the object with the path, unless absolute_result is set to False, in which
		case it returns a tuple containing the deepest object found along the path, and the
		remainder of the path after that object as a string (or None if there is no remaining
		path). Raises a DoesNotExist exception if no object is found with the given path.
		"""
		segments = path.split(pathsep)
		
		# Check for a trailing pathsep so we can restore it later.
		trailing_pathsep = False
		if segments[-1] == '':
			trailing_pathsep = True
		
		# Clean out blank segments. Handles multiple consecutive pathseps.
		while True:
			try:
				segments.remove('')
			except ValueError:
				break
		
		# Special-case a lack of segments. No queries necessary.
		if not segments:
			if root is not None:
				return root, None
			else:
				raise self.model.DoesNotExist('%s matching query does not exist.' % self.model._meta.object_name)
		
		def make_query_kwargs(segments):
			kwargs = {}
			prefix = ""
			revsegs = list(segments)
			revsegs.reverse()
			
			for segment in revsegs:
				kwargs["%s%s__exact" % (prefix, field)] = segment
				prefix += "parent__"
			
			kwargs[prefix[:-2]] = root
			return kwargs
		
		def build_path(segments):
			path = pathsep.join(segments)
			if trailing_pathsep and segments and segments[-1] != '':
				path += pathsep
			return path
		
		def find_obj(segments, depth, deepest_found):
			try:
				obj = self.get(**make_query_kwargs(segments[:depth]))
			except self.model.DoesNotExist:
				if absolute_result:
					raise
				
				depth = (deepest_found + depth)/2
				if deepest_found == depth:
					# This should happen if nothing is found with any part of the given path.
					raise
				
				# Try finding one with half the path since the deepest find.
				return find_obj(segments, depth, deepest_found)
			else:
				# Yay! Found one! Could there be a deeper one?
				if absolute_result:
					return obj
				
				deepest_found = depth
				depth = (len(segments) + depth)/2
				
				if deepest_found == depth:
					return obj, build_path(segments[deepest_found:]) or None
				
				try:
					return find_obj(segments, depth, deepest_found)
				except self.model.DoesNotExist:
					# Then the deepest one was already found.
					return obj, build_path(segments[deepest_found:])
		
		return find_obj(segments, len(segments), 0)


class TreeModel(models.Model):
	objects = TreeManager()
	parent = models.ForeignKey('self', related_name='children', null=True, blank=True)
	slug = models.SlugField(max_length=255)
	
	def has_ancestor(self, ancestor, inclusive=False):
		if inclusive:
			parent = self
		else:
			parent = self.parent
		
		parents = []
		
		while parent:
			if parent == ancestor:
				return True
			# If we've found this parent before, the path is recursive and ancestor wasn't on it.
			if parent in parents:
				return False
			parents.append(parent)
			parent = parent.parent
		# If ancestor is None, catch it here.
		if parent == ancestor:
			return True
		return False
	
	def get_path(self, root=None, pathsep='/', field='slug'):
		parent = self.parent
		parents = [self]
		
		def compile_path(parents):
			return pathsep.join([getattr(parent, field, '?') for parent in parents])
		
		while parent and parent != root:
			if parent in parents:
				if root is not None:
					raise AncestorDoesNotExist(root)
				parents.append(parent)
				return u"\u2026%s%s" % (pathsep, compile_path(parents[::-1]))
			parents.append(parent)
			parent = parent.parent
		
		if root is not None and parent is None:
			raise AncestorDoesNotExist(root)
		
		return compile_path(parents[::-1])
	path = property(get_path)
	
	def __unicode__(self):
		return self.path
	
	class Meta:
		unique_together = (('parent', 'slug'),)
		abstract = True


class TreeEntity(Entity, TreeModel):
	@property
	def attributes(self):
		if self.parent:
			return QuerySetMapper(self.attribute_set.all(), passthrough=self.parent.attributes)
		return super(TreeEntity, self).attributes
	
	class Meta:
		abstract = True