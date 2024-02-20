
import pandas as pd
import numpy as np
import setuptools
import openml
from sklearn.linear_model import LogisticRegression 
import lightgbm as lgbm
import optuna
from scipy.spatial.distance import mahalanobis
from sklearn.cluster import KMeans
from sklearn.ensemble import RandomForestClassifier
from sklearn.gaussian_process.kernels import Matern
from engression import engression, engression_bagged
import torch
from sklearn.preprocessing import StandardScaler
from scipy.spatial.distance import mahalanobis
from scipy.stats import norm
from sklearn.metrics import mean_squared_error
from rtdl_revisiting_models import MLP, ResNet, FTTransformer
import random
import gpytorch
import tqdm.auto as tqdm
import os
from pygam import LogisticGAM, s
import torch
from torch import nn
from torch.optim import Adam
from sklearn.metrics import log_loss
from sklearn.preprocessing import LabelEncoder 
from utils import EarlyStopping, train, train_trans, train_no_early_stopping, train_trans_no_early_stopping,train_GP

#SUITE_ID = 336 # Regression on numerical features
SUITE_ID = 337 # Classification on numerical features
#SUITE_ID = 335 # Regression on numerical and categorical features
#SUITE_ID = 334 # Classification on numerical and categorical features
benchmark_suite = openml.study.get_suite(SUITE_ID)  # obtain the benchmark suite

task_id=361055
task = openml.tasks.get_task(task_id)  # download the OpenML task
dataset = task.get_dataset()

X, y, categorical_indicator, attribute_names = dataset.get_data(
        dataset_format="dataframe", target=dataset.default_target_attribute)

# Transform y to int type, to then be able to apply BCEWithLogitsLoss
# Create a label encoder
le = LabelEncoder()
# Fit the label encoder and transform y to get binary labels
y_encoded = le.fit_transform(y)
# Convert the result back to a pandas Series
y = pd.Series(y_encoded, index=y.index)

# Set the random seed for reproducibility
N_TRIALS=100
N_SAMPLES=100
PATIENCE=40
N_EPOCHS=1000
GP_ITERATIONS=1000
seed=10
torch.cuda.manual_seed_all(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)
random.seed(seed)


# calculate the mean and covariance matrix of the dataset
mean = np.mean(X, axis=0)
cov = np.cov(X.T)

# calculate the Mahalanobis distance for each data point
mahalanobis_dist = [mahalanobis(x, mean, np.linalg.inv(cov)) for x in X.values]

mahalanobis_dist=pd.Series(mahalanobis_dist,index=X.index)
far_index=mahalanobis_dist.index[np.where(mahalanobis_dist>=np.quantile(mahalanobis_dist,0.8))[0]]
close_index=mahalanobis_dist.index[np.where(mahalanobis_dist<np.quantile(mahalanobis_dist,0.8))[0]]

X_train = X.loc[close_index,:]
X_test = X.loc[far_index,:]
y_train = y.loc[close_index]
y_test = y.loc[far_index]

mean = np.mean(X_train, axis=0)
cov = np.cov(X_train.T)

# calculate the Mahalanobis distance for each data point
mahalanobis_dist_ = [mahalanobis(x, mean, np.linalg.inv(cov)) for x in X_train.values]

mahalanobis_dist_=pd.Series(mahalanobis_dist_,index=X_train.index)
far_index_=mahalanobis_dist_.index[np.where(mahalanobis_dist_>=np.quantile(mahalanobis_dist_,0.8))[0]]
close_index_=mahalanobis_dist_.index[np.where(mahalanobis_dist_<np.quantile(mahalanobis_dist_,0.8))[0]]

X_train_ = X_train.loc[close_index_,:]
X_val = X_train.loc[far_index_,:]
y_train_ = y_train.loc[close_index_]
y_val = y_train.loc[far_index_]


# Convert data to PyTorch tensors
X_train__tensor = torch.tensor(X_train_.values, dtype=torch.float32)
y_train__tensor = torch.tensor(y_train_.values, dtype=torch.float32)
X_train_tensor = torch.tensor(X_train.values, dtype=torch.float32)
y_train_tensor = torch.tensor(y_train.values, dtype=torch.float32)
X_val_tensor = torch.tensor(X_val.values, dtype=torch.float32)
y_val_tensor = torch.tensor(y_val.values, dtype=torch.float32)
X_test_tensor = torch.tensor(X_test.values, dtype=torch.float32)
y_test_tensor = torch.tensor(y_test.values, dtype=torch.float32)

# Convert to use GPU if available
if torch.cuda.is_available():
    X_train__tensor = X_train__tensor.cuda()
    y_train__tensor = y_train__tensor.cuda()
    X_train_tensor = X_train_tensor.cuda()
    y_train_tensor = y_train_tensor.cuda()
    X_val_tensor = X_val_tensor.cuda()
    y_val_tensor = y_val_tensor.cuda()
    X_test_tensor = X_test_tensor.cuda()
    y_test_tensor = y_test_tensor.cuda()

# Create flattened versions of the data
y_val_np = y_val.values.flatten()
y_test_np = y_test.values.flatten()

#### Gaussian process
# Define your model
class ExactGPModel(gpytorch.models.ExactGP):
    def __init__(self, train_x, train_y, likelihood, kernel):
        super(ExactGPModel, self).__init__(train_x, train_y, likelihood)
        self.mean_module = gpytorch.means.ConstantMean()
        self.covar_module = kernel

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)

# Define the learning params
training_iterations = GP_ITERATIONS

# Define the kernels
kernels = [
    gpytorch.kernels.ScaleKernel(gpytorch.kernels.MaternKernel(nu=0.5, ard_num_dims=X_train_.shape[1])),
    gpytorch.kernels.ScaleKernel(gpytorch.kernels.MaternKernel(nu=1.5, ard_num_dims=X_train_.shape[1])),
    gpytorch.kernels.ScaleKernel(gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=X_train_.shape[1])),
    gpytorch.kernels.ScaleKernel(gpytorch.kernels.RBFKernel(ard_num_dims=X_train_.shape[1])),
]

best_log_loss = float('inf')
best_kernel = None


for kernel in kernels:
    # Initialize the Gaussian Process model and likelihood
    likelihood = gpytorch.likelihoods.BernoulliLikelihood()
    model = ExactGPModel(X_train__tensor, y_train__tensor, likelihood, kernel)

    if torch.cuda.is_available():
        model = model.cuda()

    # Use the adam optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    # "Loss" for GPs - the marginal log likelihood
    mll = gpytorch.mlls.VariationalELBO(likelihood, model, num_data=len(y_train__tensor))

    # Train the model
    train_GP(model,X_train__tensor,y_train__tensor,training_iterations,mll,optimizer)
    
    # Set the model in evaluation mode
    model.eval()
    likelihood.eval()

    # Make predictions on the validation set
    with torch.no_grad(), gpytorch.settings.fast_pred_var():
        output = model(X_val_tensor)
        preds = likelihood(output)

    # Calculate log loss
    current_log_loss = log_loss(y_val_tensor.cpu().numpy(), preds.mean.cpu().numpy())

    # Update the best kernel if the current kernel has a lower log loss
    if current_log_loss < best_log_loss:
        best_log_loss = current_log_loss
        best_kernel = kernel

# Set the random seed for reproducibility

class ExactGPModel(gpytorch.models.ExactGP):
    def __init__(self, train_x, train_y, likelihood):
        super(ExactGPModel, self).__init__(train_x, train_y, likelihood)
        self.mean_module = gpytorch.means.ConstantMean()
        self.covar_module = best_kernel

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)

# Define the learning params
training_iterations = GP_ITERATIONS

# Initialize the Gaussian Process model and likelihood
likelihood = gpytorch.likelihoods.BernoulliLikelihood()
model = ExactGPModel(X_train_tensor, y_train_tensor, likelihood)

# Use the adam optimizer
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

# "Loss" for GPs - the marginal log likelihood
mll = gpytorch.mlls.VariationalELBO(likelihood, model, num_data=len(y_train_tensor))

if torch.cuda.is_available():
    model = model.cuda()

# Train the model
train_GP(model,X_train_tensor,y_train_tensor,training_iterations,mll,optimizer)

# Set the model in evaluation mode
model.eval()
likelihood.eval()

# Make predictions on the test set
with torch.no_grad(), gpytorch.settings.fast_pred_var():
    output = model(X_test_tensor)
    preds = likelihood(output)

# Calculate log loss
log_loss_GP = log_loss(y_test_tensor.cpu().numpy(), preds.mean.cpu().numpy())
print("Log Loss GP: ", log_loss_GP)


# #### MLP
d_out = 1  
d_in=X_train_.shape[1]

def MLP_opt(trial):

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)

    n_blocks = trial.suggest_int("n_blocks", 1, 5)
    d_block = trial.suggest_int("d_block", 10, 500)
    dropout = trial.suggest_float("dropout", 0, 1)

    MLP_model = MLP(
    d_in=d_in,
    d_out=1,  # For binary classification, output dimension should be 1
    n_blocks=n_blocks,
    d_block=d_block,
    dropout=dropout,
    )
    n_epochs=N_EPOCHS
    learning_rate=trial.suggest_float('learning_rate', 0.0001, 0.05, log=True)
    weight_decay=trial.suggest_float('weight_decay', 1e-8, 1e-3, log=True)
    optimizer=torch.optim.Adam(MLP_model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    criterion = torch.nn.BCEWithLogitsLoss()  # Use Binary Cross Entropy loss for binary classification
    loss_Adam=[]

    if torch.cuda.is_available():
        MLP_model = MLP_model.cuda()
    
    early_stopping = EarlyStopping(patience=PATIENCE, verbose=False)
    n_epochs=train(MLP_model, criterion, loss_Adam, optimizer, n_epochs, X_train__tensor, y_train__tensor, X_val_tensor, y_val_tensor, early_stopping)
    n_epochs = trial.suggest_int('n_epochs', n_epochs, n_epochs)

    # Point prediction
    y_val_hat_MLP = torch.sigmoid(MLP_model(X_val_tensor).reshape(-1,))  # Apply sigmoid to get probabilities
    log_loss_MLP = log_loss(y_val_tensor.cpu().numpy(), y_val_hat_MLP.cpu().numpy())  # Calculate log loss

    return log_loss_MLP

sampler_MLP = optuna.samplers.TPESampler(seed=seed)
study_MLP = optuna.create_study(sampler=sampler_MLP, direction='minimize')  # We want to minimize log loss
study_MLP.optimize(MLP_opt, n_trials=N_TRIALS)

MLP_model = MLP(
    d_in=d_in,
    d_out=1,  # For binary classification, output dimension should be 1
    n_blocks=study_MLP.best_params['n_blocks'],
    d_block=study_MLP.best_params['d_block'],
    dropout=study_MLP.best_params['dropout'],
    )

if torch.cuda.is_available():
    MLP_model = MLP_model.cuda()
    
n_epochs=study_MLP.best_params['n_epochs']
learning_rate=study_MLP.best_params['learning_rate']
weight_decay=study_MLP.best_params['weight_decay']
optimizer=torch.optim.Adam(MLP_model.parameters(), lr=learning_rate, weight_decay=weight_decay)
criterion = torch.nn.BCEWithLogitsLoss()  # Use Binary Cross Entropy loss for binary classification
loss_Adam=[]

train_no_early_stopping(MLP_model,criterion,loss_Adam,optimizer,n_epochs,X_train_tensor,y_train_tensor)

# Point prediction
y_test_hat_MLP = torch.sigmoid(MLP_model(X_test_tensor).reshape(-1,))  # Apply sigmoid to get probabilities
log_loss_MLP = log_loss(y_test_tensor.cpu().numpy(), y_test_hat_MLP.cpu().numpy())  # Calculate log loss
print("Log Loss MLP: ", log_loss_MLP)

# #### ResNet
d_out = 1  
d_in=X_train_.shape[1]

def ResNet_opt(trial):

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)

    n_blocks = trial.suggest_int("n_blocks", 1, 5)
    d_block = trial.suggest_int("d_block", 10, 500)
    dropout1 = trial.suggest_float("dropout1", 0, 1)
    dropout2 = trial.suggest_float("dropout2", 0, 1)
    d_hidden_multiplier=trial.suggest_float("d_hidden_multiplier", 0.5, 3)

    ResNet_model = ResNet(
    d_in=d_in,
    d_out=1,  # For binary classification, output dimension should be 1
    n_blocks=n_blocks,
    d_block=d_block,
    d_hidden=None,
    d_hidden_multiplier=d_hidden_multiplier,
    dropout1=dropout1,
    dropout2=dropout2,
    )
    if torch.cuda.is_available():
        ResNet_model = ResNet_model.cuda()
    n_epochs=N_EPOCHS
    learning_rate=trial.suggest_float('learning_rate', 0.0001, 0.05, log=True)
    weight_decay=trial.suggest_float('weight_decay', 1e-8, 1e-3, log=True)
    optimizer=torch.optim.Adam(ResNet_model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    criterion = torch.nn.BCEWithLogitsLoss()  # Use Binary Cross Entropy loss for binary classification
    loss_Adam=[]

    early_stopping = EarlyStopping(patience=PATIENCE, verbose=False)
    n_epochs=train(ResNet_model, criterion, loss_Adam, optimizer, n_epochs, X_train__tensor, y_train__tensor, X_val_tensor, y_val_tensor, early_stopping)
    n_epochs = trial.suggest_int('n_epochs', n_epochs, n_epochs)

    # Point prediction
    y_val_hat_ResNet = torch.sigmoid(ResNet_model(X_val_tensor).reshape(-1,))  # Apply sigmoid to get probabilities
    log_loss_ResNet = log_loss(y_val_tensor.cpu().numpy(), y_val_hat_ResNet.cpu().numpy())  # Calculate log loss

    return log_loss_ResNet

sampler_ResNet = optuna.samplers.TPESampler(seed=seed)
study_ResNet = optuna.create_study(sampler=sampler_ResNet, direction='minimize')  # We want to minimize log loss
study_ResNet.optimize(ResNet_opt, n_trials=N_TRIALS)

ResNet_model = ResNet(
    d_in=d_in,
    d_out=1,  # For binary classification, output dimension should be 1
    n_blocks=study_ResNet.best_params['n_blocks'],
    d_block=study_ResNet.best_params['d_block'],
    d_hidden=None,
    d_hidden_multiplier=study_ResNet.best_params['d_hidden_multiplier'],
    dropout1=study_ResNet.best_params['dropout1'],
    dropout2=study_ResNet.best_params['dropout2'],
    )

if torch.cuda.is_available():
    ResNet_model = ResNet_model.cuda()

n_epochs=study_ResNet.best_params['n_epochs']
learning_rate=study_ResNet.best_params['learning_rate']
weight_decay=study_ResNet.best_params['weight_decay']
optimizer=torch.optim.Adam(ResNet_model.parameters(), lr=learning_rate, weight_decay=weight_decay)
criterion = torch.nn.BCEWithLogitsLoss()  # Use Binary Cross Entropy loss for binary classification
loss_Adam=[]

train_no_early_stopping(ResNet_model,criterion,loss_Adam,optimizer,n_epochs,X_train_tensor,y_train_tensor)

# Point prediction
y_test_hat_ResNet = torch.sigmoid(ResNet_model(X_test_tensor).reshape(-1,))  # Apply sigmoid to get probabilities
log_loss_ResNet = log_loss(y_test_tensor.cpu().numpy(), y_test_hat_ResNet.cpu().numpy())  # Calculate log loss
print("Log Loss ResNet: ", log_loss_ResNet)

# #### FFTransformer

d_out = 1  
d_in=X_train_.shape[1]

def FTTrans_opt(trial):

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)

    n_blocks = trial.suggest_int("n_blocks", 1, 5)
    d_block_multiplier = trial.suggest_int("d_block_multiplier", 1, 25)
    attention_n_heads = trial.suggest_int("attention_n_heads", 1, 20)
    attention_dropout = trial.suggest_float("attention_dropout", 0, 1)
    ffn_d_hidden_multiplier=trial.suggest_float("ffn_d_hidden_multiplier", 0.5, 3)
    ffn_dropout = trial.suggest_float("ffn_dropout", 0, 1)
    residual_dropout = trial.suggest_float("residual_dropout", 0, 1)

    FTTrans_model = FTTransformer(
    n_cont_features=d_in,
    cat_cardinalities=[],
    d_out=1,  # For binary classification, output dimension should be 1
    n_blocks=n_blocks,
    d_block=d_block_multiplier*attention_n_heads,
    attention_n_heads=attention_n_heads,
    attention_dropout=attention_dropout,
    ffn_d_hidden=None,
    ffn_d_hidden_multiplier=ffn_d_hidden_multiplier,
    ffn_dropout=ffn_dropout,
    residual_dropout=residual_dropout,
    )

    if torch.cuda.is_available():
        FTTrans_model = FTTrans_model.cuda()

    n_epochs=N_EPOCHS
    learning_rate=trial.suggest_float('learning_rate', 0.0001, 0.05, log=True)
    weight_decay=trial.suggest_float('weight_decay', 1e-8, 1e-3, log=True)
    optimizer=torch.optim.Adam(FTTrans_model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    criterion = torch.nn.BCEWithLogitsLoss()  # Use Binary Cross Entropy loss for binary classification
    loss_Adam=[]

    early_stopping = EarlyStopping(patience=PATIENCE, verbose=False)
    n_epochs=train_trans(FTTrans_model, criterion, loss_Adam, optimizer, n_epochs, X_train__tensor, y_train__tensor, X_val_tensor, y_val_tensor, early_stopping)
    n_epochs = trial.suggest_int('n_epochs', n_epochs, n_epochs)

    # Point prediction
    y_val_hat_FTTrans = torch.sigmoid(FTTrans_model(X_val_tensor, None).reshape(-1,))  # Apply sigmoid to get probabilities
    log_loss_FTTrans = log_loss(y_val_tensor.cpu().numpy(), y_val_hat_FTTrans.cpu().numpy())  # Calculate log loss

    return log_loss_FTTrans

sampler_FTTrans = optuna.samplers.TPESampler(seed=seed)
study_FTTrans = optuna.create_study(sampler=sampler_FTTrans, direction='minimize')  # We want to minimize log loss
study_FTTrans.optimize(FTTrans_opt, n_trials=N_TRIALS)


FTTrans_model = FTTransformer(
    n_cont_features=d_in,
    cat_cardinalities=[],
    d_out=1,  # For binary classification, output dimension should be 1
    n_blocks=study_FTTrans.best_params['n_blocks'],
    d_block=study_FTTrans.best_params['d_block_multiplier']*study_FTTrans.best_params['attention_n_heads'],
    attention_n_heads=study_FTTrans.best_params['attention_n_heads'],
    attention_dropout=study_FTTrans.best_params['attention_dropout'],
    ffn_d_hidden=None,
    ffn_d_hidden_multiplier=study_FTTrans.best_params['ffn_d_hidden_multiplier'],
    ffn_dropout=study_FTTrans.best_params['ffn_dropout'],
    residual_dropout=study_FTTrans.best_params['residual_dropout'],
    )

if torch.cuda.is_available():
    FTTrans_model = FTTrans_model.cuda()

n_epochs=study_FTTrans.best_params['n_epochs']
learning_rate=study_FTTrans.best_params['learning_rate']
weight_decay=study_FTTrans.best_params['weight_decay']
optimizer=torch.optim.Adam(FTTrans_model.parameters(), lr=learning_rate, weight_decay=weight_decay)
criterion = torch.nn.BCEWithLogitsLoss()  # Use Binary Cross Entropy loss for binary classification
loss_Adam=[]

train_trans_no_early_stopping(FTTrans_model,criterion,loss_Adam,optimizer,n_epochs,X_train_tensor,y_train_tensor)

# Point prediction
y_test_hat_FTTrans = torch.sigmoid(FTTrans_model(X_test_tensor, None).reshape(-1,))  # Apply sigmoid to get probabilities
log_loss_FTTrans = log_loss(y_test_tensor.cpu().numpy(), y_test_hat_FTTrans.cpu().numpy())  # Calculate log loss
print("Log Loss FTTrans: ", log_loss_FTTrans)

# #### Boosted trees, random forest, engression, linear regression

def boosted(trial):

    params = {'learning_rate': trial.suggest_float('learning_rate', 0.001, 0.5, log=True),
              'n_estimators': trial.suggest_int('n_estimators', 100, 500),
              'reg_lambda': trial.suggest_float('reg_lambda', 1e-8, 10.0, log=True),
              'max_depth': trial.suggest_int('max_depth', 1, 30),
              'min_child_samples': trial.suggest_int('min_child_samples', 10, 100)}
    
    boosted_tree_model=lgbm.LGBMClassifier(**params)
    boosted_tree_model.fit(X_train_, y_train_)
    y_val_hat_boost = boosted_tree_model.predict_proba(X_val)[:, 1]  # Get predicted probabilities
    log_loss_boost = log_loss(y_val, y_val_hat_boost)  # Calculate log loss

    return log_loss_boost

sampler_boost = optuna.samplers.TPESampler(seed=seed)
study_boost = optuna.create_study(sampler=sampler_boost, direction='minimize')  # We want to minimize log loss
study_boost.optimize(boosted, n_trials=N_TRIALS)
boosted_model=lgbm.LGBMClassifier(**study_boost.best_params)

def rf(trial):

    params = {'n_estimators': trial.suggest_int('n_estimators', 100, 500),
              'max_depth': trial.suggest_int('max_depth', 1, 30),
              'max_features': trial.suggest_int('max_features', 1, 30),
              'min_samples_leaf': trial.suggest_int('min_samples_leaf', 10, 100)}
    
    rf_model=RandomForestClassifier(**params)
    rf_model.fit(X_train_, y_train_)
    y_val_hat_rf = rf_model.predict_proba(X_val)[:, 1]  # Get predicted probabilities
    log_loss_rf = log_loss(y_val, y_val_hat_rf)  # Calculate log loss

    return log_loss_rf

sampler_rf = optuna.samplers.TPESampler(seed=seed)
study_rf = optuna.create_study(sampler=sampler_rf, direction='minimize')
study_rf.optimize(rf, n_trials=N_TRIALS)
rf_model=RandomForestClassifier(**study_rf.best_params)

def engressor_NN(trial):

    params = {'learning_rate': trial.suggest_float('learning_rate', 0.0001, 0.01, log=True),
              'num_epoches': trial.suggest_int('num_epoches', 100, 1000),
              'num_layer': trial.suggest_int('num_layer', 2, 5),
              'hidden_dim': trial.suggest_int('hidden_dim', 100, 500),}
    params['noise_dim']=params['hidden_dim']

    # Check if CUDA is available and if so, move the tensors and the model to the GPU
    if torch.cuda.is_available():
        engressor_model=engression(X_train__tensor, y_train__tensor.reshape(-1,1), lr=params['learning_rate'], num_epoches=params['num_epoches'],num_layer=params['num_layer'], hidden_dim=params['hidden_dim'], noise_dim=params['noise_dim'], batch_size=1000, sigmoid=True, device="cuda")
    else: 
        engressor_model=engression(X_train__tensor, y_train__tensor.reshape(-1,1), lr=params['learning_rate'], num_epoches=params['num_epoches'],num_layer=params['num_layer'], hidden_dim=params['hidden_dim'], noise_dim=params['noise_dim'], batch_size=1000, sigmoid=True)
    
    # Generate a sample from the engression model for each data point
    y_val_hat_engression = engressor_model.predict(X_val_tensor, target="mean")  # Get predicted probabilities
    log_loss_engression = log_loss(y_val_tensor.cpu().numpy(), y_val_hat_engression.cpu().numpy())  # Calculate log loss

    return log_loss_engression

sampler_engression = optuna.samplers.TPESampler(seed=seed)
study_engression = optuna.create_study(sampler=sampler_engression, direction='minimize')  # We want to maximize accuracy
study_engression.optimize(engressor_NN, n_trials=N_TRIALS)


# Fit the boosted model and make predictions
boosted_model.fit(X_train, y_train)
y_test_hat_boosted = boosted_model.predict_proba(X_test)[:, 1]  # Get predicted probabilities
log_loss_boosted = log_loss(y_test, y_test_hat_boosted)  # Calculate log loss

# Fit the random forest model and make predictions
rf_model.fit(X_train, y_train)
y_test_hat_rf = rf_model.predict_proba(X_test)[:, 1]  # Get predicted probabilities
log_loss_rf = log_loss(y_test, y_test_hat_rf)  # Calculate log loss

# Fit the logistic regression model and make predictions
log_reg = LogisticRegression()
log_reg.fit(X_train, y_train)
y_test_hat_logreg = log_reg.predict_proba(X_test)[:, 1]  # Get predicted probabilities
log_loss_logreg = log_loss(y_test, y_test_hat_logreg)  # Calculate log loss

# Engression model
params=study_engression.best_params
params['noise_dim']=params['hidden_dim']
# Check if CUDA is available and if so, move the tensors and the model to the GPU
if torch.cuda.is_available():
    engressor_model=engression(X_train_tensor, y_train_tensor.reshape(-1,1), lr=params['learning_rate'], num_epoches=params['num_epoches'],num_layer=params['num_layer'], hidden_dim=params['hidden_dim'], noise_dim=params['noise_dim'], batch_size=1000, sigmoid=True, device="cuda")
else: 
    engressor_model=engression(X_train_tensor, y_train_tensor.reshape(-1,1), lr=params['learning_rate'], num_epoches=params['num_epoches'],num_layer=params['num_layer'], hidden_dim=params['hidden_dim'], noise_dim=params['noise_dim'], batch_size=1000, sigmoid=True)
# Assuming the model outputs probabilities for the two classes
y_test_hat_engression=engressor_model.predict(X_test_tensor, target="mean")
# Assuming the model outputs probabilities for the two classes
y_test_hat_engression = engressor_model.predict(X_test_tensor, target="mean")
log_loss_engression = log_loss(y_test_tensor.cpu().numpy(), y_test_hat_engression.cpu().numpy())  # Calculate log loss

print("Log Loss logistic regression: ", log_loss_logreg)
print("Log Loss boosted trees: ", log_loss_boosted)
print("Log Loss random forest: ", log_loss_rf)
print("Log Loss engression: ", log_loss_engression)

# GAM model
def gam_model(trial):

    # Define the hyperparameters to optimize
    params = {'n_splines': trial.suggest_int('n_splines', 5, 20),
              'lam': trial.suggest_loguniform('lam', 1e-3, 1)}

    # Create and train the model
    gam = LogisticGAM(s(0, n_splines=params['n_splines'], lam=params['lam'])).fit(X_train_, y_train_)

    # Predict on the validation set and calculate the log loss
    y_val_hat_gam = gam.predict_proba(X_val)
    log_loss_gam = log_loss(y_val, y_val_hat_gam)

    return log_loss_gam

# Create the sampler and study
sampler_gam = optuna.samplers.TPESampler(seed=seed)
study_gam = optuna.create_study(sampler=sampler_gam, direction='minimize')  # We want to minimize log loss

# Optimize the model
study_gam.optimize(gam_model, n_trials=N_TRIALS)

# Create the final model with the best parameters
best_params = study_gam.best_params
final_gam_model = LogisticGAM(s(0, n_splines=best_params['n_splines'], lam=best_params['lam']))

# Fit the model
final_gam_model.fit(X_train, y_train)

# Predict on the test set
y_test_hat_gam = final_gam_model.predict_proba(X_test)
# Calculate the log loss
log_loss_gam = log_loss(y_test, y_test_hat_gam)
print("Log Loss GAM: ", log_loss_gam)

log_loss_results = {'GP': log_loss_GP, 'MLP': log_loss_MLP, 'ResNet': log_loss_ResNet, 'FTTrans': log_loss_FTTrans, 'boosted_trees': log_loss_boosted, 'rf': log_loss_rf, 'linear_regression': log_loss_linreg, 'engression': log_loss_engression, 'GAM': log_loss_gam}  

# Convert the dictionary to a DataFrame
df = pd.DataFrame(list(log_loss_results.items()), columns=['Method', 'Log Loss'])

# Create the directory if it doesn't exist
os.makedirs('RESULTS/MAHALANOBIS', exist_ok=True)

# Save the DataFrame to a CSV file
df.to_csv(f'RESULTS/MAHALANOBIS/{task_id}_mahalanobis_logloss_results.csv', index=False)