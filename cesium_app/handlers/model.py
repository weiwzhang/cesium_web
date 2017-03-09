'''Handlers for '/models' route.'''

from .base import BaseHandler, AccessError
from ..models import Project, Model, Featureset, File
from ..ext.sklearn_models import (
    model_descriptions as sklearn_model_descriptions,
    check_model_param_types
    )
from ..util import robust_literal_eval
from ..config import cfg

from os.path import join as pjoin
import uuid
import datetime

from cesium import build_model, featureset
import tornado.ioloop
import joblib
import xarray as xr
from distributed.client import _wait


def _build_model_compute_statistics(fset_path, model_type, model_params,
                                    params_to_optimize, model_path):
    '''Build model and return summary statistics.

    Parameters
    ----------
    fset_path : str
        Path to feature set NetCDF file.
    model_type : str
        Type of model to be built, e.g. 'RandomForestClassifier'.
    model_params : dict
        Dictionary with hyperparameter values to be used in model building.
        Keys are parameter names, values are the associated parameter values.
        These hyperparameters will be passed to the model constructor as-is
        (for hyperparameter optimization, see `params_to_optimize`).
    params_to_optimize : dict or list of dict
        During hyperparameter optimization, various model parameters
        are adjusted to give an optimal fit. This dictionary gives the
        different values that should be explored for each parameter. E.g.,
        `{'alpha': [1, 2], 'beta': [4, 5, 6]}` would fit models on all
        6 combinations of alpha and beta and compare the resulting models'
        goodness-of-fit. If None, only those hyperparameters specified in
        `model_parameters` will be used (passed to model constructor as-is).
    model_path : str
        Path indicating where serialized model will be saved.

    Returns
    -------
    score : float
        The model's training score.
    best_params : dict
        Dictionary of best hyperparameter values (keys are parameter names,
        values are the corresponding best values) determined by `scikit-learn`'s
        `GridSearchCV`. If no hyperparameter optimization is performed (i.e.
        `params_to_optimize` is None or is an empty dict, this will be an empty
        dict.
    '''
    fset = featureset.from_netcdf(fset_path)
    computed_model = build_model.build_model_from_featureset(
        featureset=fset, model_type=model_type,
        model_parameters=model_params,
        params_to_optimize=params_to_optimize)
    score = build_model.score_model(computed_model, fset)
    best_params = computed_model.best_params_ if params_to_optimize else {}
    joblib.dump(computed_model, model_path)
    fset.close()

    return score, best_params


class ModelHandler(BaseHandler):
    def _get_model(self, model_id):
        try:
            m = Model.get(Model.id == model_id)
        except Model.DoesNotExist:
            raise AccessError('No such model')

        if not m.is_owned_by(self.get_username()):
            raise AccessError('No such project')

        return m

    def get(self, model_id=None):
        if model_id is not None:
            model_info = self._get_model(model_id)
        else:
            model_info = [model for p in Project.all(self.get_username())
                          for model in p.models]

        return self.success(model_info)

    @tornado.gen.coroutine
    def _await_model_statistics(self, model_stats_future, model):
        try:
            score, best_params = yield model_stats_future._result()

            model.task_id = None
            model.finished = datetime.datetime.now()
            model.train_score = score
            model.params.update(best_params)
            model.save()

            self.action('cesium/SHOW_NOTIFICATION',
                        payload={"note": "Model '{}' computed.".format(model.name)})

        except Exception as e:
            model.delete_instance()
            self.action('cesium/SHOW_NOTIFICATION',
                        payload={"note": "Cannot create model '{}': {}".format(model.name, e),
                                 "type": 'error'})

        self.action('cesium/FETCH_MODELS')

    @tornado.gen.coroutine
    def post(self):
        data = self.get_json()

        model_name = data.pop('modelName')
        featureset_id = data.pop('featureSet')
        # TODO remove cast once this is passed properly from the front end
        model_type = sklearn_model_descriptions[int(data.pop('modelType'))]['name']
        project_id = data.pop('project')

        fset = Featureset.get(Featureset.id == featureset_id)
        if not fset.is_owned_by(self.get_username()):
            return self.error('No access to featureset')

        if fset.finished is None:
            return self.error('Cannot build model for in-progress feature set')

        model_params = data
        model_params = {k: robust_literal_eval(v)
                        for k, v in model_params.items()}

        model_params, params_to_optimize = check_model_param_types(model_type,
                                                                   model_params)
        model_type = model_type.split()[0]
        model_path = pjoin(cfg['paths']['models_folder'],
                           '{}_model.pkl'.format(uuid.uuid4()))

        model_file = File.create(uri=model_path)
        model = Model.create(name=model_name, file=model_file,
                             featureset=fset, project=fset.project,
                             params=model_params, type=model_type)

        executor = yield self._get_executor()

        model_stats_future = executor.submit(
            _build_model_compute_statistics, fset.file.uri, model_type,
            model_params, params_to_optimize, model_path)

        model.task_id = model_stats_future.key
        model.save()

        loop = tornado.ioloop.IOLoop.current()
        loop.spawn_callback(self._await_model_statistics, model_stats_future, model)

        return self.success(data={'message': "Model training begun."},
                            action='cesium/FETCH_MODELS')


    def delete(self, model_id):
        m = self._get_model(model_id)
        m.delete_instance()

        return self.success(action='cesium/FETCH_MODELS')
