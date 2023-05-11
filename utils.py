#Import the required packages. 
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split
import pandas as pd
from sklearn.model_selection import train_test_split

class LoadingDatasets:
    def __init__(self, train=None, test=None):
        self.train = train
        self.test = test

    def load_tabular_dataset(self, file_path):
        """Loads the dataset from the specified file path and returns a pandas DataFrame."""
        if file_path.endswith('.csv'):
            return pd.read_csv(file_path)
        elif file_path.endswith('.json'):
            return pd.read_json(file_path)
        elif file_path.endswith('.parquet'):
            return pd.read_parquet(file_path)
        elif file_path.endswith('.xlsx'):
            return pd.read_excel(file_path)

    def load_split_tabular_dataset(self, file_path=None, train_file_path=None, test_file_path=None):
        """Loads the dataset from the specified file path(s) and splits it into train and test sets."""
        if file_path:
            df = self.load_tabular_dataset(file_path)
            self.train, self.test = self._split_dataset(df)
        elif train_file_path and test_file_path:
            self.train = self.load_tabular_dataset(train_file_path)
            self.test = self.load_tabular_dataset(test_file_path)
        else:
            self.train = self.load_tabular_dataset(train_file_path)
            self.test = None

    def _split_dataset(self, df):
        """Helper method that splits the dataset into train and test sets based on the problem type."""
        raise NotImplementedError
    


class ClassificationLoadingDatasets(LoadingDatasets):
    def __init__(self, target_col=None):
        super().__init__()
        self.target_col = target_col

    def _split_dataset(self, df):
        if self.target_col:
            if df[self.target_col].nunique() <= 2:
                # Binary classification task
                train, test = train_test_split(df, test_size=0.2, stratify=df[self.target_col])
            else:
                # Multiclass classification task
                train, test = train_test_split(df, test_size=0.2, stratify=df[self.target_col])
        else:
            raise ValueError("Target column must be specified for classification task.")
        return train, test

class RegressionLoadingDatasets(LoadingDatasets):
    def _split_dataset(self, df):
        train, test = train_test_split(df, test_size=0.2)
        return train, test

class TimeSeriesLoadingDatasets(LoadingDatasets):
    def __init__(self, test_start_date=None, group_col=None):
        super().__init__()
        self.test_start_date = test_start_date
        self.group_col = group_col

    def _split_dataset(self, df):
        if self.group_col:
            # Group the time series data by the specified column
            groups = df.groupby(self.group_col)
            train = pd.concat([group[:int(len(group)*0.8)] for _, group in groups])
            test = pd.concat([group[int(len(group)*0.8):] for _, group in groups])
        elif self.test_start_date:
            # Time series task
            train = df[df.index < self.test_start_date]
            test = df[df.index >= self.test_start_date]
        else:
            raise ValueError("Test start date must be specified for time series task.")

        return train, test