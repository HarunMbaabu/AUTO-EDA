## **AUTO EDA**
- Automated exploratory data analysis (EDA) Library which incorporates GPT-3.5 to explain the output to users. 




### **The library includes the following functions:**
- **load_dataset(file_path):** loads the dataset from the specified file path and returns a pandas DataFrame.
- **explore_dataset(data):** provides summary statistics and visualizations of the dataset, including the number of rows and columns, data types, missing values, and distribution of values for each column.
- **clean_dataset(data, drop_missing=True):** performs data cleaning on the dataset, including removing duplicate rows, filling missing values, and dropping columns with a high percentage of missing values. The user can choose whether to drop rows with missing values or not. 

```python 

import pandas as pd
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt

def load_dataset(file_path):
    """Loads the dataset from the specified file path and returns a pandas DataFrame."""
    if file_path.endswith('.csv'):
        return pd.read_csv(file_path)
    elif file_path.endswith('.json'):
        return pd.read_json(file_path)
    elif file_path.endswith('.xlsx'):
        return pd.read_excel(file_path)

def explore_dataset(data):
    """Provides summary statistics and visualizations of the dataset."""
    print("Dataset shape:", data.shape)
    print("Dataset data types:\n", data.dtypes)
    print("Number of missing values:\n", data.isna().sum())

    # Visualize the distribution of values for each column
    for col in data.columns:
        if data[col].dtype == 'object':
            sns.countplot(data[col])
            plt.title(col)
            plt.show()
        else:
            sns.histplot(data[col])
            plt.title(col)
            plt.show()

def clean_dataset(data, drop_missing=True):
    """Performs data cleaning on the dataset."""
    # Remove duplicate rows
    data.drop_duplicates(inplace=True)

    # Fill missing values
    data.fillna(method='ffill', inplace=True)

    # Drop columns with a high percentage of missing values
    missing_percent = data.isnull().sum() / len(data) * 100
    to_drop = list(missing_percent[missing_percent > 50].index)
    data.drop(to_drop, axis=1, inplace=True)

    # Drop rows with missing values if drop_missing is True
    if drop_missing:
        data.dropna(inplace=True)
        
    return data
``` 

How to use:

```python
# Load the dataset
data = load_dataset('my_data.csv')

# Explore the dataset
explore_dataset(data)

# Clean the dataset
clean_data = clean_dataset(data, drop_missing=True) 
```
