"""
Scorecard development.
"""

# Guillermo Navas-Palencia <g.navas.palencia@gmail.com>
# Copyright (C) 2020

import numbers
import time

import numpy as np
import pandas as pd

from sklearn.base import BaseEstimator
from sklearn.base import clone
from sklearn.exceptions import NotFittedError
from sklearn.utils.multiclass import type_of_target

from ..binning.binning_process import BinningProcess
from ..logging import Logger
from .rounding import RoundingMIP


def _check_parameters(target, binning_process, estimator, scaling_method,
                      scaling_method_data, intercept_based, reverse_scorecard,
                      rounding, verbose):

    if not isinstance(target, str):
        raise TypeError("target must be a string.")

    if not isinstance(binning_process, BinningProcess):
        raise TypeError("binning_process must be a BinningProcess instance.")

    if not isinstance(estimator, object):
        raise TypeError("estimator must be an object with methods fit and "
                        "predict.")

    if not hasattr(estimator, "fit"):
        raise TypeError("estimator must be an object with methods fit and "
                        "predict.")

    if not hasattr(estimator, "predict"):
        raise TypeError("estimator must be an object with methods fit and "
                        "predict.")

    if scaling_method is not None:
        if scaling_method not in ("pdo_odds", "min_max"):
            raise ValueError('Invalid value for scaling_method. Allowed '
                             'string values are "pd_odds" and "min_max".')

        if scaling_method_data is None:
            raise ValueError("scaling_method_data cannot be None if "
                             "scaling_method is provided.")

        if not isinstance(scaling_method_data, dict):
            raise TypeError("scaling_method_data must be a dict.")

    if not isinstance(intercept_based, bool):
        raise TypeError("intercept_based must be a boolean; got {}."
                        .format(intercept_based))

    if not isinstance(reverse_scorecard, bool):
        raise TypeError("reverse_scorecard must be a boolean; got {}."
                        .format(reverse_scorecard))

    if not isinstance(rounding, bool):
        raise TypeError("rounding must be a boolean; got {}.".format(rounding))

    if not isinstance(verbose, bool):
        raise TypeError("verbose must be a boolean; got {}.".format(verbose))


def _check_scorecard_scaling(scaling_method, scaling_method_data, target_type):
    if scaling_method is not None:
        if scaling_method == "pdo_odds":
            default_keys = ["pdo", "odds", "scorecard_points"]

            if target_type != "binary":
                raise ValueError('scaling_method "pd_odds" is not supported '
                                 'for a continuous target.')

        elif scaling_method == "min_max":
            default_keys = ["min", "max"]

        if set(scaling_method_data.keys()) != set(default_keys):
            raise ValueError("scaling_method_data must be {} given "
                             "scaling_method = {}."
                             .format(default_keys, scaling_method))

        if scaling_method == "pdo_odds":
            for param in default_keys:
                value = scaling_method_data[param]
                if not isinstance(value, numbers.Number) or value <= 0:
                    raise ValueError("{} must be a positive number; got {}."
                                     .format(param, value))

        elif scaling_method == "min_max":
            for param in default_keys:
                value = scaling_method_data[param]
                if not isinstance(value, numbers.Number):
                    raise ValueError("{} must be numeric; got {}."
                                     .format(param, value))

            if scaling_method_data["min"] > scaling_method_data["max"]:
                raise ValueError("min must be <= max; got {} <= {}."
                                 .format(scaling_method_data["min"],
                                         scaling_method_data["max"]))


def _compute_scorecard_points(points, binning_tables, method, method_data,
                              intercept, reverse_scorecard):
    """Apply scaling method to scorecard."""
    n = len(binning_tables)

    sense = -1 if reverse_scorecard else 1

    if method == "pdo_odds":
        pdo = method_data["pdo"]
        odds = method_data["odds"]
        scorecard_points = method_data["scorecard_points"]

        factor = pdo / np.log(2)
        offset = scorecard_points - factor * np.log(odds)

        new_points = -(sense * points + intercept / n) * factor + offset / n
    elif method == "min_max":
        a = method_data["min"]
        b = method_data["max"]

        min_p = np.sum([np.min(bt.Points) for bt in binning_tables])
        max_p = np.sum([np.max(bt.Points) for bt in binning_tables])

        smin = intercept + min_p
        smax = intercept + max_p

        slope = sense * (a - b) / (smax - smin)
        if reverse_scorecard:
            shift = a - slope * smin
        else:
            shift = b - slope * smin

        base_points = shift + slope * intercept
        new_points = base_points / n + slope * points

    return new_points


def _compute_intercept_based(df_scorecard):
    """Compute an intercept-based scorecard.

    All points within a variable are adjusted so that the lowest point is zero.
    """
    scaled_points = np.zeros(df_scorecard.shape[0])
    selected_variables = df_scorecard.Variable.unique()
    intercept = 0
    for variable in selected_variables:
        mask = df_scorecard.Variable == variable
        points = df_scorecard[mask].Points.values
        min_point = np.min(points)
        scaled_points[mask] = points - min_point
        intercept += min_point

    return scaled_points, intercept


class Scorecard(BaseEstimator):
    """Scorecard.

    Parameters
    ----------
    target : str

    binning_process : object

    estimator : object

    scaling_method : str or None (default=None)

    scaling_method_data : dict or None (default=None)

    intercept_based : bool (default=False)

    rounding : bool (default=False)

    verbose : bool (default=False)
        Enable verbose output.

    Attributes
    ----------
    binning_process_ : object
        The external binning process.

    estimator_ : object
        The external estimator fit on the reduced dataset.

    intercept_ : float
        The intercept if ``intercept_based=True``.
    """
    def __init__(self, target, binning_process, estimator, scaling_method=None,
                 scaling_method_data=None, intercept_based=False,
                 reverse_scorecard=True, rounding=False, verbose=True):

        self.target = target
        self.binning_process = binning_process
        self.estimator = estimator
        self.scaling_method = scaling_method
        self.scaling_method_data = scaling_method_data
        self.intercept_based = intercept_based
        self.reverse_scorecard = reverse_scorecard
        self.rounding = rounding
        self.verbose = verbose

        # attributes
        self.binning_process_ = None
        self.estimator_ = None
        self.intercept_ = 0

        # auxiliary
        self._target_dtype = None

        # timing
        self._time_total = None
        self._time_binning_process = None
        self._time_estimator = None
        self._time_build_scorecard = None
        self._time_rounding = None

        # logger
        self._class_logger = Logger(__name__)
        self._logger = self._class_logger.logger

        self._is_fitted = False

    def fit(self, df, metric_special=0, metric_missing=0, show_digits=2,
            check_input=False):
        """Fit scorecard.

        Parameters
        ----------
        df : pandas.DataFrame (n_samples, n_features)
            Training vector, where n_samples is the number of samples.

        metric_special : float or str (default=0)
            The metric value to transform special codes in the input vector.
            Supported metrics are "empirical" to use the empirical WoE or
            event rate, and any numerical value.

        metric_missing : float or str (default=0)
            The metric value to transform missing values in the input vector.
            Supported metrics are "empirical" to use the empirical WoE or
            event rate and any numerical value.

        check_input : bool (default=False)
            Whether to check input arrays.

        show_digits : int, optional (default=2)
            The number of significant digits of the bin column.

        Returns
        -------
        self : object
            Fitted scorecard.
        """
        return self._fit(df, metric_special, metric_missing, show_digits,
                         check_input)

    def information(self, print_level=1):
        """Print overview information about the options settings and
        statistics.

        Parameters
        ----------
        print_level : int (default=1)
            Level of details.
        """
        self._check_is_fitted()

        if not isinstance(print_level, numbers.Integral) or print_level < 0:
            raise ValueError("print_level must be an integer >= 0; got {}."
                             .format(print_level))

    def predict(self, df):
        """

        Parameters
        ----------
        df : pandas.DataFrame (n_samples, n_features)
            Training vector, where n_samples is the number of samples.

        Returns
        -------
        """
        self._check_is_fitted()

        df_t = df[self.binning_process_.variable_names]
        df_t = self.binning_process_.transform(df_t)
        return self.estimator_.predict(df_t)

    def predict_proba(self, df):
        """

        Parameters
        ----------
        df : pandas.DataFrame (n_samples, n_features)
            Training vector, where n_samples is the number of samples.

        Returns
        -------
        """
        self._check_is_fitted()

        df_t = df[self.binning_process_.variable_names]
        df_t = self.binning_process_.transform(df_t)
        return self.estimator_.predict_proba(df_t)

    def score(self, df):
        """

        Parameters
        ----------
        df : pandas.DataFrame (n_samples, n_features)
            Training vector, where n_samples is the number of samples.

        Returns
        -------
        """
        self._check_is_fitted()

        df_t = df[self.binning_process_.variable_names]
        df_t = self.binning_process_.transform(df_t, metric="indices")

        score_ = np.zeros(df_t.shape[0])
        selected_variables = self.binning_process_.get_support(names=True)

        for variable in selected_variables:
            mask = self._df_scorecard.Variable == variable
            points = self._df_scorecard[mask].Points.values
            score_ += points[df_t[variable]]

        return score_ + self.intercept_

    def table(self, style="summary"):
        """Scorecard table.

        Parameters
        ----------
        style : str (default="summary")

        Returns
        -------
        table : pandas.DataFrame
        """
        self._check_is_fitted()

        if style not in ("summary", "detailed"):
            raise ValueError('Invalid value for style. Allowed string '
                             'values are "summary" and "detailed".')

        if style == "summary":
            columns = ["Variable", "Bin", "Points"]
        elif style == "detailed":
            main_columns = ["Variable", "Bin id", "Bin"]
            columns = self._df_scorecard.columns
            rest_columns = [col for col in columns if col not in main_columns]
            columns = main_columns + rest_columns

        return self._df_scorecard[columns]

    def _fit(self, df, metric_special, metric_missing, show_digits,
             check_input):

        time_init = time.perf_counter()

        if self.verbose:
            self._logger.info("Scorecard building process started.")
            self._logger.info("Options: check parameters.")

        _check_parameters(**self.get_params(deep=False))

        # Target type and metric
        target = df[self.target]
        self._target_dtype = type_of_target(target)

        if self._target_dtype not in ("binary", "continuous"):
            raise ValueError("Target type {} is not supported."
                             .format(self._target_dtype))

        _check_scorecard_scaling(self.scaling_method, self.scaling_method_data,
                                 self._target_dtype)

        if self._target_dtype == "binary":
            metric = "woe"
            bt_metric = "WoE"
        elif self._target_dtype == "continuous":
            metric = "mean"
            bt_metric = "Mean"

        if self.verbose:
            self._logger.info("Dataset: {} target.".format(self._target_dtype))

        # Fit binning process
        if self.verbose:
            self._logger.info("Binning process started.")

        time_binning_process = time.perf_counter()
        self.binning_process_ = clone(self.binning_process)
        # Suppress binning process verbosity
        self.binning_process_.set_params(verbose=False)

        df_t = self.binning_process_.fit_transform(
            df[self.binning_process.variable_names], target,
            metric, metric_special, metric_missing, show_digits,
            check_input)

        self._time_binning_process = time.perf_counter() - time_binning_process

        if self.verbose:
            self._logger.info("Binning process terminated. Time: {:.4f}s"
                              .format(self._time_binning_process))

        # Fit estimator
        time_estimator = time.perf_counter()
        if self.verbose:
            self._logger.info("Fitting estimator.")

        self.estimator_ = clone(self.estimator)
        self.estimator_.fit(df_t, target)

        self._time_estimator = time.perf_counter() - time_estimator

        if self.verbose:
            self._logger.info("Fitting terminated. Time {:.4f}s"
                              .format(self._time_estimator))

        # Get coefs
        intercept = 0
        if hasattr(self.estimator_, 'coef_'):
            coefs = self.estimator_.coef_
            if hasattr(self.estimator_, 'intercept_'):
                intercept = self.estimator_.intercept_
        else:
            raise RuntimeError('The classifier does not expose '
                               '"coef_" attribute.')

        # Build scorecard
        time_build_scorecard = time.perf_counter()

        if self.verbose:
            self._logger.info("Scorecard table building started.")

        selected_variables = self.binning_process_.get_support(names=True)
        binning_tables = []
        for i, variable in enumerate(selected_variables):
            optb = self.binning_process_.get_binned_variable(variable)
            binning_table = optb.binning_table.build(add_totals=False)
            c = coefs.ravel()[i]
            binning_table["Variable"] = variable
            binning_table["Coefficient"] = c
            binning_table["Points"] = binning_table[bt_metric] * c
            binning_table.index.names = ['Bin id']
            binning_table.reset_index(level=0, inplace=True)
            binning_tables.append(binning_table)

        df_scorecard = pd.concat(binning_tables)
        df_scorecard.reset_index()

        # Apply score points
        if self.scaling_method is not None:
            points = df_scorecard["Points"]
            scaled_points = _compute_scorecard_points(
                points, binning_tables, self.scaling_method,
                self.scaling_method_data, intercept, self.reverse_scorecard)

            df_scorecard["Points"] = scaled_points

            if self.intercept_based:
                scaled_points, self.intercept_ = _compute_intercept_based(
                    df_scorecard)
                df_scorecard["Points"] = scaled_points

        if self.rounding:
            points = df_scorecard["Points"]
            if self.scaling_method == "pdo_odds":
                round_points = np.rint(points)
            elif self.scaling_method == "min_max":
                round_mip = RoundingMIP()
                round_mip.build_model(df_scorecard)
                status, round_points = round_mip.solve()

                if status not in ("OPTIMAL", "FEASIBLE"):
                    # Add self._logger message
                    # Back-up method
                    round_points = np.rint(points)

            df_scorecard["Points"] = round_points

        self._df_scorecard = df_scorecard

        self._time_build_scorecard = time.perf_counter() - time_build_scorecard
        self._time_total = time.perf_counter() - time_init

        if self.verbose:
            self._logger.info("Scorecard table terminated. Time: {:.4f}s"
                              .format(self._time_build_scorecard))
            self._logger.info("Scorecard building process terminated. Time: "
                              "{:.4f}s".format(self._time_total))

        # Completed successfully
        self._class_logger.close()
        self._is_fitted = True

        return self

    def _check_is_fitted(self):
        if not self._is_fitted:
            raise NotFittedError("This {} instance is not fitted yet. Call "
                                 "'fit' with appropriate arguments."
                                 .format(self.__class__.__name__))
