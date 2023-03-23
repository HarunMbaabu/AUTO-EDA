from setuptools import setup, find_packages


 setup(
        name="DSEAfricaAutoClean",

        version="0.1.0",
        packages=find_packages(),
        install_requires=[
            "numpy",
            "pandas", 
            "seaborn",
            "matplotlib",
            "scikit-learn"
        ],
        description="Python library with code example to perform EDA and data cleaning.",
 )
  

