import pyforest

from main import load_dataset, explore_dataset, clean_dataset  


data = load_dataset("Data/houses-for-rent.json") 

print(data.head(2)) 


#Use the explore dataset method
print(explore_dataset(data)) 


#Use the clean_dataset method
print(clean_dataset(data)) 


#Print the last 2 rows 
print(data.tail(2)) 