#!/usr/bin/env python

from django.conf import settings
from django.core.exceptions import MultipleObjectsReturned 
from django.core.urlresolvers import reverse
from django.utils import simplejson as json
from tastypie import fields, http
from tastypie.authorization import DjangoAuthorization
from tastypie.bundle import Bundle
from tastypie.exceptions import BadRequest, NotFound, ImmediateHttpResponse
from tastypie.resources import Resource
from tastypie.utils.mime import build_content_type
from tastypie.validation import Validation

from redd import solr
from redd.api.datasets import DatasetResource
from redd.api.utils import CustomApiKeyAuthentication, CustomPaginator, CustomSerializer
from redd.models import Dataset

class SolrObject(object):
    """
    A lightweight wrapper around a Solr response object for use when
    querying Solr via Tastypie.
    """
    def __init__(self, initial=None, **kwargs):
        self.__dict__['_data'] = {}

        if hasattr(initial, 'items'):
            self.__dict__['_data'] = initial

        self.__dict__['_data'].update(kwargs)

    def __getattr__(self, name):
        return self._data.get(name, None)

    def __setattr__(self, name, value):
        self.__dict__['_data'][name] = value

    def __str__(self):
        return str(self.__dict__['_data'])

    def __unicode__(self):
        return unicode(self.__dict__['_data'])

    def to_dict(self):
        return self._data

class DataValidation(Validation):
    """
    Tastypie Validation for Data objects.
    """
    def is_valid(self, bundle, request=None):
        errors = {}

        if 'data' not in bundle.data or not bundle.data['data']:
            errors['data'] = ['This field is required.']
            return errors

        return errors

class DataResource(Resource):
    """
    API resource for data.
    """
    id = fields.CharField(attribute='id',
        help_text='Unique id of this row of data.')
    dataset_slug = fields.CharField(attribute='dataset_slug',
        help_text='Slug of the dataset this row of data belongs to.')
    external_id = fields.CharField(attribute='external_id', null=True, blank=True,
        help_text='Per-dataset unique identifier for this row of data.')
    data = fields.CharField(attribute='data',
        help_text='An ordered list of values corresponding to the columns in the parent dataset.')

    class Meta:
        resource_name = 'data'
        allowed_methods = ['get', 'post', 'put', 'delete']
        always_return_data = True

        authentication = CustomApiKeyAuthentication()
        authorization = DjangoAuthorization()
        serializer = CustomSerializer()
        validation = DataValidation()

        object_class = SolrObject

    def dehydrate_data(self, bundle):
        """
        Convert csv data into a proper array for JSON serialization
        """
        return json.loads(bundle.data['data'])

    def dehydrate(self, bundle):
        """
        Trim the dataset_slug field and add a proper relationship.
        """
        from redd.api.datasets import DatasetResource

        dataset = Dataset.objects.get(slug=bundle.data['dataset_slug'])
        dr = DatasetResource()
        uri = dr.get_resource_uri(dataset)

        del bundle.data['dataset_slug']
        bundle.data['dataset'] = uri

        return bundle

    def get_resource_uri(self, bundle_or_obj):
        """
        Build a canonical uri for a datum.
        """
        dr = DatasetResource()

        kwargs = {
            'api_name': self._meta.api_name,
            # TODO: should be dynamic
            'dataset_resource_name': 'dataset',
            'resource_name': self._meta.resource_name,
        }

        if isinstance(bundle_or_obj, Bundle):
            kwargs['dataset_slug'] = bundle_or_obj.obj.dataset_slug
            kwargs['external_id'] = bundle_or_obj.obj.external_id
        else:
            kwargs['dataset_slug'] = bundle_or_obj.dataset_slug
            kwargs['external_id'] = bundle_or_obj.external_id

        return dr._build_reverse_url('api_dataset_data_detail', kwargs=kwargs)

    def get_dataset_from_kwargs(self, bundle, **kwargs):
        """
        Extract a dataset from one of the variety of places it might be hiding.

        TODO: doesn't need to actually fetch Datasets in order to compare slugs
        """
        kwargs_slug = kwargs['dataset_slug']
        kwargs_dataset = Dataset.objects.get(slug=kwargs_slug)

        bundle_uri = bundle.data.pop('dataset', None)
        bundle_dataset = None

        if bundle_uri:
            bundle_dataset = DatasetResource().get_via_uri(bundle_uri)

        if bundle_dataset and kwargs_dataset:
            if bundle_dataset.id != kwargs_dataset.id:
                raise BadRequest('Dataset specified in request body does not agree with dataset API endpoint used.')

        return bundle_dataset or kwargs_dataset

    def validate_bundle_data(self, bundle, request, dataset):
        """
        Perform additional validation that isn't possible with the Validation object.
        """
        errors = {}

        field_count = len(bundle.data['data'])

        if dataset.data_upload and not dataset.imported:
            errors['dataset'] = ['Can not create or modify data for a dataset which has data_upload, but has not completed the import process.']

        if dataset.schema is None:
            errors['dataset'] = ['Can not create or modify data for a dataset without a schema.']
        else:
            expected_field_count = len(dataset.schema)

            if field_count != expected_field_count:
                errors['data'] = ['Got %i data fields. Expected %i.' % (field_count, expected_field_count)]

        # Cribbed from is_valid()
        if errors:
            if request:
                desired_format = self.determine_format(request)
            else:
                desired_format = self._meta.default_format

            serialized = self.serialize(request, errors, desired_format)
            response = http.HttpBadRequest(content=serialized, content_type=build_content_type(desired_format))
            raise ImmediateHttpResponse(response=response)

    # Data access methods

    def get_object_list():
        """
        Bypassed, should never be invoked. 

        Since Solr queries are not lazy, fetching a complete list
        of objects never makes sense.
        """
        raise NotImplementedError() 

    def obj_get_list(self, request=None, **kwargs):
        """
        Bypassed, should never be invoked. 
        
        See ``get_list``.
        """
        raise NotImplementedError() 

    def obj_get(self, request=None, **kwargs):
        """
        Query Solr for a single item by primary key.
        """
        dataset = Dataset.objects.get(slug=kwargs['dataset_slug'])
        dataset_slug = unicode(dataset.slug)
    
        response = solr.query(settings.SOLR_DATA_CORE, 'dataset_slug:%s external_id:%s' % (dataset_slug, kwargs['external_id']))

        if len(response['response']['docs']) < 1:
            raise NotFound()

        if len(response['response']['docs']) > 1:
            raise MultipleObjectsReturned()

        return SolrObject(response['response']['docs'][0])

    def obj_create(self, bundle, request=None, **kwargs):
        """
        Add one Data to a Dataset.

        TODO: committing everytime this is called doesn't make sense, especially
        since this function is called multiple times in put_list().
        """
        dataset = self.get_dataset_from_kwargs(bundle, **kwargs)

        self.validate_bundle_data(bundle, request, dataset)

        if 'external_id' in bundle.data:
            external_id = bundle.data['external_id']
        else:
            external_id = None

        row = dataset.add_row(bundle.data['data'], external_id=external_id)

        bundle.obj = SolrObject(row)

        return bundle

    def obj_update(self, bundle, request=None, **kwargs):
        """
        Update an existing Data.
        """
        dataset = self.get_dataset_from_kwargs(bundle, **kwargs)
        
        # Verify it exists
        self.obj_get(request, **kwargs)
        
        self.validate_bundle_data(bundle, request, dataset)

        if 'external_id' in bundle.data:
            external_id = bundle.data['external_id']
        else:
            external_id = kwargs['external_id'] 

        row = dataset.update_row(external_id, bundle.data['data'])
        
        bundle.obj = SolrObject(row)

        return bundle

    def obj_delete_list(self, request=None, **kwargs):
        """
        A bit of a hack: this prevents ``put_list() from nuking the collection prior
        to creating/updating Data.
        """
        pass

    def obj_delete(self, request=None, **kwargs):
        """
        Delete a ``Data``.

        TODO: See note in ``obj_create`` about committing.
        """
        response = solr.query(settings.SOLR_DATA_CORE, 'dataset_slug:%s external_id:%s' % (kwargs['dataset_slug'], kwargs['external_id']))

        if len(response['response']['docs']) < 1:
            raise NotFound()

        if len(response['response']['docs']) > 1:
            raise MultipleObjectsReturned()

        dataset = Dataset.objects.get(slug=kwargs['dataset_slug'])

        solr.delete(settings.SOLR_DATA_CORE, 'dataset_slug:%s external_id:%s' % (kwargs['dataset_slug'], kwargs['external_id']), commit=True)

        dataset.row_count -= 1
        dataset.save()

    # Views

    def get_list(self, request, **kwargs):
        """
        Retrieve a list of ``Data`` objects, optionally applying full-text search.

        Bypasses ``obj_get_list``, making it unnecessary.
        """
        results = self.search_dataset_data(request, **kwargs)

        return self.create_response(request, results)

    def get_detail(self, request, **kwargs):
        """
        Handled by the underlying implementation.

        See ``obj_get``.
        """
        return super(DataResource, self).get_detail(request, **kwargs)

    def put_list(self, request, **kwargs):
        """
        Handled by the underlying implementation, however, note that
        this version does not delete all ``Data`` prior to creating
        new ones.

        See ``obj_delete_list`` and ``obj_create``.
        """
        return super(DataResource, self).put_list(request, **kwargs)

    def put_detail(self, request, **kwargs):
        """
        Handled by the underlying implementation.

        See ``obj_update``.
        """
        return super(DataResource, self).put_detail(request, **kwargs)

    def post_list(self, request, **kwargs):
        """
        Handled by the underlying implementation.

        See ``obj_create``.
        """
        return super(DataResource, self).post_list(request, **kwargs)

    def post_detail(self, request, **kwargs):
        """
        Handled by the underlying implementation, which means this is
        not supported.
        """
        return super(DataResource, self).post_detail(request, **kwargs)

    def delete_list(self, request, **kwargs):
        """
        Delete all ``Data`` in a ``Dataset``. Must be called from a data
        url nested under a Dataset. Deleting *all* ``Data`` objects is
        not supported.
        """
        dataset = Dataset.objects.get(slug=kwargs['dataset_slug'])

        solr.delete(settings.SOLR_DATA_CORE, 'dataset_slug:%s' % dataset.slug, commit=True)

        dataset.row_count = 0
        dataset.save()

        return http.HttpNoContent()

    def delete_detail(self, request, **kwargs):
        return super(DataResource, self).delete_detail(request, **kwargs)

    # Search

    def search_all_data(self, request, **kwargs):
        """
        List endpoint using Solr. Provides full-text search via the "q" parameter."
        """
        query = request.GET.get('q', '')
        limit = int(request.GET.get('limit', settings.PANDA_DEFAULT_SEARCH_GROUPS))
        offset = int(request.GET.get('offset', 0))
        group_limit = int(request.GET.get('group_limit', settings.PANDA_DEFAULT_SEARCH_ROWS_PER_GROUP))
        group_offset = int(request.GET.get('group_offset', 0))

        response = solr.query_grouped(
            settings.SOLR_DATA_CORE,
            query,
            'dataset_slug',
            offset=offset,
            limit=limit,
            group_limit=group_limit,
            group_offset=group_offset
        )
        groups = response['grouped']['dataset_slug']['groups']

        page = CustomPaginator(
            request.GET,
            groups,
            resource_uri=request.path_info,
            count=response['grouped']['dataset_slug']['ngroups']
        ).page()

        datasets = []

        for group in groups:
            dataset_slug = group['groupValue']
            results = group['doclist']

            dataset_resource = DatasetResource()
            dataset = Dataset.objects.get(slug=dataset_slug)
            dataset_bundle = dataset_resource.build_bundle(obj=dataset, request=request)
            dataset_bundle = dataset_resource.full_dehydrate(dataset_bundle)
            dataset_bundle = dataset_resource.simplify_bundle(dataset_bundle)

            objects = [SolrObject(obj) for obj in results['docs']]
            
            dataset_search_url = reverse('api_dataset_data_list', kwargs={ 'api_name': self._meta.api_name, 'dataset_resource_name': 'dataset', 'resource_name': 'data', 'dataset_slug': dataset.slug })

            data_page = CustomPaginator(
                { 'limit': str(group_limit), 'offset': str(group_offset), 'q': query },
                objects,
                resource_uri=dataset_search_url,
                count=results['numFound']
            ).page()

            dataset_bundle.data.update(data_page)
            dataset_bundle.data['objects'] = []

            for obj in objects:
                data_bundle = self.build_bundle(obj=obj, request=request)
                data_bundle = self.full_dehydrate(data_bundle)
                dataset_bundle.data['objects'].append(data_bundle)

            datasets.append(dataset_bundle.data)

        page['objects'] = datasets

        return self.create_response(request, page)

    def search_dataset_data(self, request, **kwargs):
        """
        Perform a full-text search on only one dataset.

        See ``get_list``.
        """
        dataset = Dataset.objects.get(slug=kwargs['dataset_slug'])

        query = request.GET.get('q', '')
        limit = int(request.GET.get('limit', settings.PANDA_DEFAULT_SEARCH_ROWS))
        offset = int(request.GET.get('offset', 0))

        response = solr.query(
            settings.SOLR_DATA_CORE,
            'dataset_slug:%s %s' % (dataset.slug, query),
            offset=offset,
            limit=limit
        )

        dataset_resource = DatasetResource()
        dataset_bundle = dataset_resource.build_bundle(obj=dataset, request=request)
        dataset_bundle = dataset_resource.full_dehydrate(dataset_bundle)
        dataset_bundle = dataset_resource.simplify_bundle(dataset_bundle)
       
        results = [SolrObject(d) for d in response['response']['docs']]

        page = CustomPaginator(
            request.GET,
            results,
            resource_uri=request.path_info,
            count=response['response']['numFound']
        ).page() 
        
        dataset_bundle.data.update(page)
        dataset_bundle.data['objects'] = []

        for obj in results:
            bundle = self.build_bundle(obj=obj, request=request)
            bundle = self.full_dehydrate(bundle)
            dataset_bundle.data['objects'].append(bundle.data)

        return dataset_bundle

