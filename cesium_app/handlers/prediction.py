from .base import BaseHandler, AccessError
from ..models import Prediction, File, Dataset, Model, Project
from ..config import cfg
from .. import util

import tornado.gen
from tornado.web import RequestHandler
from tornado.escape import json_decode

import cesium.time_series
import cesium.featurize
import cesium.predict
import cesium.featureset
from cesium.features import CADENCE_FEATS, GENERAL_FEATS, LOMB_SCARGLE_FEATS

import xarray as xr
import joblib
from os.path import join as pjoin
import uuid
import datetime
import os
import tempfile


class PredictionHandler(BaseHandler):
    def _get_prediction(self, prediction_id):
        try:
            d = Prediction.get(Prediction.id == prediction_id)
        except Prediction.DoesNotExist:
            raise AccessError('No such dataset')

        if not d.is_owned_by(self.get_username()):
            raise AccessError('No such dataset')

        return d

    @tornado.gen.coroutine
    def _await_prediction(self, future, prediction):
        try:
            result = yield future._result()

            prediction.task_id = None
            prediction.finished = datetime.datetime.now()
            prediction.save()

            self.action('cesium/SHOW_NOTIFICATION',
                        payload={
                            "note": "Prediction '{}/{}' completed.".format(
                                prediction.dataset.name,
                                prediction.model.name)
                            })

        except Exception as e:
            prediction.delete_instance()
            self.action('cesium/SHOW_NOTIFICATION',
                        payload={
                            "note": "Prediction '{}/{}'" " failed "
                            "with error {}. Please try again.".format(
                                prediction.dataset.name,
                                prediction.model.name, e),
                             "type": "error"
                            })

        self.action('cesium/FETCH_PREDICTIONS')

    @tornado.gen.coroutine
    def post(self):
        data = self.get_json()

        dataset_id = data['datasetID']
        model_id = data['modelID']

        dataset = Dataset.get(Dataset.id == data["datasetID"])
        model = Model.get(Model.id == data["modelID"])

        username = self.get_username()

        if not (dataset.is_owned_by(username) and model.is_owned_by(username)):
            return self.error('No access to dataset or model')

        fset = model.featureset
        if (model.finished is None) or (fset.finished is None):
            return self.error('Computation of model or feature set still in progress')

        prediction_path = pjoin(cfg['paths']['predictions_folder'],
                                '{}_prediction.nc'.format(uuid.uuid4()))
        prediction_file = File.create(uri=prediction_path)
        prediction = Prediction.create(file=prediction_file, dataset=dataset,
                                       project=dataset.project, model=model)

        executor = yield self._get_executor()

        all_time_series = executor.map(cesium.time_series.from_netcdf,
                                       dataset.uris)
        all_features = executor.map(cesium.featurize.featurize_single_ts,
                                    all_time_series,
                                    features_to_use=fset.features_list,
                                    custom_script_path=fset.custom_features_script)
        fset_data = executor.submit(cesium.featurize.assemble_featureset,
                                    all_features, all_time_series)
        fset_data = executor.submit(cesium.featureset.Featureset.impute, fset_data)
        model_data = executor.submit(joblib.load, model.file.uri)
        predset = executor.submit(cesium.predict.model_predictions,
                                  fset_data, model_data)
        future = executor.submit(xr.Dataset.to_netcdf, predset, prediction_path)

        prediction.task_id = future.key
        prediction.save()

        loop = tornado.ioloop.IOLoop.current()
        loop.spawn_callback(self._await_prediction, future, prediction)

        return self.success(prediction.display_info(), 'cesium/FETCH_PREDICTIONS')

    def get(self, prediction_id=None, action=None):
        if action == 'download':
            prediction = cesium.featureset.from_netcdf(self._get_prediction(prediction_id).file.uri)
            with tempfile.NamedTemporaryFile() as tf:
                util.prediction_to_csv(prediction, tf.name)
                with open(tf.name) as f:
                    self.set_header("Content-Type", 'text/csv; charset="utf-8"')
                    self.set_header("Content-Disposition",
                                    "attachment; filename=cesium_prediction_results.csv")
                    self.write(f.read())
        else:
            if prediction_id is None:
                predictions = [prediction
                               for project in Project.all(self.get_username())
                               for prediction in project.predictions]
                prediction_info = [p.display_info() for p in predictions]
            else:
                prediction = self._get_prediction(prediction_id)
                prediction_info = prediction.display_info()

            return self.success(prediction_info)

    def delete(self, prediction_id):
        prediction = self._get_prediction(prediction_id)
        prediction.delete_instance()
        return self.success(action='cesium/FETCH_PREDICTIONS')


class PredictRawDataHandler(BaseHandler):
    def post(self):
        ts_data = json_decode(self.get_argument('ts_data'))
        model_id = json_decode(self.get_argument('modelID'))
        meta_feats = json_decode(
            self.get_argument('meta_features', 'null'))
        impute_kwargs = json_decode(
            self.get_argument('impute_kwargs', '{}'))

        model = Model.get(Model.id == model_id)
        computed_model = joblib.load(model.file.uri)
        features_to_use = model.featureset.features_list

        fset_data = cesium.featurize.featurize_time_series(
            *ts_data, features_to_use=features_to_use, meta_features=meta_feats)
        fset = cesium.featureset.Featureset(fset_data).impute(**impute_kwargs)

        predset = cesium.predict.model_predictions(fset, computed_model)
        predset['name'] = predset.name.astype('str')

        return self.success(predset)
