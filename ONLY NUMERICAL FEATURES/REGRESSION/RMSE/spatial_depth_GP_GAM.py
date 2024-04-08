import pandas as pd
import numpy as np
import setuptools
import openml
from sklearn.linear_model import LinearRegression 
import lightgbm as lgbm
import optuna
from scipy.spatial.distance import mahalanobis
from sklearn.cluster import KMeans
from sklearn.ensemble import RandomForestRegressor
from sklearn.gaussian_process.kernels import Matern
from engression import engression, engression_bagged
import torch
from sklearn.preprocessing import StandardScaler
from scipy.spatial.distance import mahalanobis
from scipy.stats import norm
from sklearn.metrics import mean_squared_error
from rtdl_revisiting_models import MLP, ResNet, FTTransformer
from properscoring import crps_gaussian, crps_ensemble
import random
import gpytorch
import tqdm.auto as tqdm
import os
from pygam import LinearGAM, s, f
from utils import EarlyStopping, train, train_trans, train_no_early_stopping, train_trans_no_early_stopping
from torch.utils.data import TensorDataset, DataLoader
import re
import shutil
import gpboost as gpb

import rpy2.robjects as robjects
from rpy2.robjects import pandas2ri
from rpy2.robjects.packages import importr

# Create the checkpoint directory if it doesn't exist
if os.path.exists('CHECKPOINTS/SPATIAL_DEPTH'):
    shutil.rmtree('CHECKPOINTS/SPATIAL_DEPTH')
os.makedirs('CHECKPOINTS/SPATIAL_DEPTH')

SUITE_ID = 336 # Regression on numerical features
#SUITE_ID = 337 # Classification on numerical features
#SUITE_ID = 335 # Regression on numerical and categorical features
#SUITE_ID = 334 # Classification on numerical and categorical features
benchmark_suite = openml.study.get_suite(SUITE_ID)  # obtain the benchmark suite

#task_id=361072
for task_id in benchmark_suite.tasks:

    if task_id<=361072:
        continue

    if task_id==361084:
        continue

    # Set the random seed for reproducibility
    N_TRIALS=100
    N_SAMPLES=100
    PATIENCE=40
    N_EPOCHS=1000
    GP_ITERATIONS=1000
    BATCH_SIZE=1024
    seed=10
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    random.seed(seed)

    print(f"Task {task_id}")

    CHECKPOINT_PATH = f'CHECKPOINTS/SPATIAL_DEPTH/task_{task_id}.pt'

    task = openml.tasks.get_task(task_id)  # download the OpenML task
    dataset = task.get_dataset()

    X, y, categorical_indicator, attribute_names = dataset.get_data(
            dataset_format="dataframe", target=dataset.default_target_attribute)
    
    if len(X) > 15000:
        indices = np.random.choice(X.index, size=15000, replace=False)
        X = X.iloc[indices,]
        y = y[indices]

    # Remove categorical columns with more than 20 unique values and non-categorical columns with less than 10 unique values
    # Remove non-categorical columns with more than 70% of the data in one category from X_clean
    for col in [attribute for attribute, indicator in zip(attribute_names, categorical_indicator) if indicator]:
        if len(X[col].unique()) > 20:
            X = X.drop(col, axis=1)

    X_clean=X.copy()
    for col in [attribute for attribute, indicator in zip(attribute_names, categorical_indicator) if not indicator]:
        if len(X[col].unique()) < 10:
            X = X.drop(col, axis=1)
            X_clean = X_clean.drop(col, axis=1)
        elif X[col].value_counts(normalize=True).max() > 0.7:
            X_clean = X_clean.drop(col, axis=1)

    # Find features with absolute correlation > 0.9
    corr_matrix = X_clean.corr().abs()
    upper_tri = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))
    high_corr_features = [column for column in upper_tri.columns if any(upper_tri[column] > 0.9)]

    # Drop one of the highly correlated features from X_clean
    X_clean = X_clean.drop(high_corr_features, axis=1)

    # Rename columns to avoid problems with LGBM
    X = X.rename(columns = lambda x:re.sub('[^A-Za-z0-9_]+', '', x))

    # activate pandas conversion for rpy2
    pandas2ri.activate()

    # import R's "ddalpha" package
    ddalpha = importr('ddalpha')

    # explicitly import the projDepth function
    spatialDepth = robjects.r['depth.spatial']

    # calculate the spatial depth for each data point
    spatial_depth = spatialDepth(X_clean, X_clean)

    spatial_depth=pd.Series(spatial_depth,index=X_clean.index)
    far_index=spatial_depth.index[np.where(spatial_depth<=np.quantile(spatial_depth,0.2))[0]]
    close_index=spatial_depth.index[np.where(spatial_depth>np.quantile(spatial_depth,0.2))[0]]

    X_train_clean = X_clean.loc[close_index,:]
    X_train = X.loc[close_index,:]
    X_test = X.loc[far_index,:]
    y_train = y.loc[close_index]
    y_test = y.loc[far_index]

    # convert the R vector to a pandas Series
    spatial_depth_ = spatialDepth(X_train_clean, X_train_clean)

    spatial_depth_=pd.Series(spatial_depth_,index=X_train_clean.index)
    far_index_=spatial_depth_.index[np.where(spatial_depth_<=np.quantile(spatial_depth_,0.2))[0]]
    close_index_=spatial_depth_.index[np.where(spatial_depth_>np.quantile(spatial_depth_,0.2))[0]]

    X_train_ = X_train.loc[close_index_,:]
    X_val = X_train.loc[far_index_,:]
    y_train_ = y_train.loc[close_index_]
    y_val = y_train.loc[far_index_]


    # Standardize the data
    mean_X_train_ = np.mean(X_train_, axis=0)
    std_X_train_ = np.std(X_train_, axis=0)
    X_train_ = (X_train_ - mean_X_train_) / std_X_train_
    X_val = (X_val - mean_X_train_) / std_X_train_

    mean_X_train = np.mean(X_train, axis=0)
    std_X_train = np.std(X_train, axis=0)
    X_train = (X_train - mean_X_train) / std_X_train
    X_test = (X_test - mean_X_train) / std_X_train


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
        print("Using GPU")
        X_train__tensor = X_train__tensor.cuda()
        y_train__tensor = y_train__tensor.cuda()
        X_train_tensor = X_train_tensor.cuda()
        y_train_tensor = y_train_tensor.cuda()
        X_val_tensor = X_val_tensor.cuda()
        y_val_tensor = y_val_tensor.cuda()
        X_test_tensor = X_test_tensor.cuda()
        y_test_tensor = y_test_tensor.cuda()
    else:
        print("Using CPU")

    # Create flattened versions of the data
    y_val_np = y_val.values.flatten()
    y_test_np = y_test.values.flatten()

    # Create TensorDatasets for training and validation sets
    train__dataset = TensorDataset(X_train__tensor, y_train__tensor)
    train_dataset = TensorDataset(X_train_tensor, y_train_tensor)
    val_dataset = TensorDataset(X_val_tensor, y_val_tensor)
    test_dataset = TensorDataset(X_test_tensor, y_test_tensor)

    # Create DataLoaders for training and validation sets
    train__loader = DataLoader(train__dataset, batch_size=BATCH_SIZE, shuffle=True)
    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False)

    # Define d_out and d_in
    d_out = 1  
    d_in=X_train_.shape[1]

    #### GP model
    if task_id==361073:
        RMSE_GP = float("NaN")
    else:
        approximations = ["vecchia", "fitc"]
        kernels = ["matern_ard", "gaussian_ard"]
        shapes = [0.5, 1.5, 2.5]
        best_RMSE = float('inf')    
        intercept_train=np.ones(X_train_.shape[0])
        intercept_val=np.ones(X_val.shape[0])
        for approx in approximations:
            for kernel in kernels:
                if kernel=="matern_ard":
                    for shape in shapes:
                        gp_model = gpb.GPModel(gp_coords=X_train_, cov_function=kernel, cov_fct_shape=shape, likelihood="gaussian", gp_approx=approx)
                        gp_model.fit(y=y_train_, X=intercept_train, params={"trace": True})
                        pred_resp = gp_model.predict(gp_coords_pred=X_val, X_pred=intercept_val, predict_var=True, predict_response=True)['mu']
                        RMSE_GP = np.sqrt(np.mean((y_val-pred_resp)**2))
                        print("RMSE GP temporary: ", RMSE_GP)
                        if RMSE_GP < best_RMSE:
                            best_RMSE = RMSE_GP
                            best_approx = approx
                            best_kernel = kernel
                            best_shape = shape
                else:
                    gp_model = gpb.GPModel(gp_coords=X_train_, cov_function=kernel, likelihood="gaussian", gp_approx=approx)
                    gp_model.fit(y=y_train_, X=intercept_train, params={"trace": True})
                    pred_resp = gp_model.predict(gp_coords_pred=X_val, X_pred=intercept_val, predict_var=True, predict_response=True)['mu']
                    RMSE_GP = np.sqrt(np.mean((y_val-pred_resp)**2))
                    print("RMSE GP temporary: ", RMSE_GP)
                    if RMSE_GP < best_RMSE:
                        best_RMSE = RMSE_GP
                        best_approx = approx
                        best_kernel = kernel
                        best_shape = None
        
        intercept_train=np.ones(X_train.shape[0])
        intercept_test=np.ones(X_test.shape[0])
        if best_kernel=="matern_ard":
            gp_model = gpb.GPModel(gp_coords=X_train, cov_function=best_kernel, cov_fct_shape=best_shape, likelihood="gaussian", gp_approx=best_approx)
        else:
            gp_model = gpb.GPModel(gp_coords=X_train, cov_function=best_kernel, likelihood="gaussian", gp_approx=best_approx)
        
        gp_model.fit(y=y_train, X=intercept_train, params={"trace": True})
        pred_resp = gp_model.predict(gp_coords_pred=X_test, X_pred=intercept_test, predict_var=True, predict_response=True)['mu']
        RMSE_GP = np.sqrt(np.mean((y_test-pred_resp)**2))    
    print("RMSE GP: ", RMSE_GP)

    #### GAM model
    def gam_model(trial):

        # Define the search space for n_splines, lam, and spline_order
        n_splines=trial.suggest_int('n_splines', 10, 100)
        lam=trial.suggest_float('lam', 1e-3, 1e3, log=True)
        spline_order=trial.suggest_int('spline_order', 1, 5)
        
        # Create and train the model
        gam = LinearGAM(n_splines=n_splines, spline_order=spline_order, lam=lam).fit(X_train_, y_train_)

        # Predict on the validation set and calculate the RMSE
        y_val_hat_gam = gam.predict(X_val)
        RMSE_gam = np.sqrt(np.mean((y_val - y_val_hat_gam) ** 2))

        return RMSE_gam

    # Create the sampler and study
    sampler_gam = optuna.samplers.TPESampler(seed=seed)
    study_gam = optuna.create_study(sampler=sampler_gam, direction='minimize')

    # Optimize the model
    study_gam.optimize(gam_model, n_trials=N_TRIALS)

    # Get the best parameters
    best_params = study_gam.best_params
    n_splines=best_params['n_splines']
    lam=best_params['lam']
    spline_order=best_params['spline_order']

    final_gam_model = LinearGAM(n_splines=n_splines, spline_order=spline_order, lam=lam)

    # Fit the model
    final_gam_model.fit(X_train, y_train)

    # Predict on the test set
    y_test_hat_gam = final_gam_model.predict(X_test)
    # Calculate the RMSE
    RMSE_gam = np.sqrt(np.mean((y_test - y_test_hat_gam) ** 2))
    print("RMSE GAM: ", RMSE_gam)

    # Load the existing DataFrame
    df = pd.read_csv(f'RESULTS/SPATIAL_DEPTH/{task_id}_spatial_depth_RMSE_results.csv')

    # Add the columns with RMSE of GAM and GP
    df.loc[df['Method'] == 'GAM', 'RMSE'] = RMSE_gam
    df.loc[len(df)] = ['GP', RMSE_GP]

    # Create the directory if it doesn't exist
    os.makedirs('RESULTS2/SPATIAL_DEPTH', exist_ok=True)

    # Save the DataFrame to a CSV file
    df.to_csv(f'RESULTS2/SPATIAL_DEPTH/{task_id}_spatial_depth_RMSE_results.csv', index=False)