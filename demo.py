import pyforest

from main import load_dataset, explore_dataset, clean_dataset  


data = load_dataset("data/Iris.csv") 

print(data.head(2)) 


#Use the explore dataset method
print(explore_dataset(data)) 


#Use the clean_dataset method
print(clean_dataset(data))

