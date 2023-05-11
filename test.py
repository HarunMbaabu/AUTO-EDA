import pandas as pd
from sklearn.model_selection import train_test_split
from utils import (LoadingDatasets, ClassificationLoadingDatasets, 
                   RegressionLoadingDatasets, TimeSeriesLoadingDatasets)

def main():
    file_type = input("What type of file(s) do you have? (.csv, .json, .parquet, .xlsx) ")
    if file_type not in ['.csv', '.json', '.parquet', '.xlsx']:
        raise ValueError("Invalid file type.")
    
    # Ask the user for the type of files they have
    file_path_type = input("Do you have:\n1. One file\n2. Separate train and test files\nEnter 1 or 2: ")

    if file_path_type == "1":
        # Ask for the file path and load the dataset
        file_path = input("Enter the path to the file: ")
        df = LoadingDatasets().load_tabular_dataset(file_path)

        # Ask the user if they want to split the dataset into train and test sets
        split_dataset = input("Do you want to split the dataset into train and test sets? (y/n): ")

        if split_dataset.lower() == "y":
            # Check the type of problem and split the dataset accordingly
            problem_type = input("What type of problem are you trying to solve? (classification/regression/time series): ")

            if problem_type.lower() == "classification":
                target_col = input("Enter the name of the target column: ")
                train, test = ClassificationLoadingDatasets(target_col)._split_dataset(df)

            elif problem_type.lower() == "regression":
                train, test = RegressionLoadingDatasets()._split_dataset(df)

            elif problem_type.lower() == "time series":
                test_start_date = input("Enter the test start date (YYYY-MM-DD): ")
                group_col = input("Enter the name of the group column (press enter if none): ")
                train, test = TimeSeriesLoadingDatasets(test_start_date, group_col)._split_dataset(df)

            else:
                raise ValueError("Invalid problem type specified.")

        else:
            # Use the whole dataset as the training set
            train = df
            test = None

    elif file_path_type == "2":
        # Ask for the file paths for the train and test datasets
        train_file_path = input("Enter the path to the train file: ")
        test_file_path = input("Enter the path to the test file: ")

        # Load the train and test datasets
        train = LoadingDatasets().load_tabular_dataset(train_file_path)
        test = LoadingDatasets().load_tabular_dataset(test_file_path)

    else:
        raise ValueError("Invalid file type specified.")
    
    print("Shape of the training dataset: ", train.shape)
    print("Shape of the testing dataset: ", test.shape)

if __name__ == '__main__':
    main()
