#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Main routine for retrieving reference MeteoSwiss data (e.g. CPC, RZC, POH, etc)
This is meant to be run as a command line command from a slurm script

i.e. ./retrieve_reference_data -t <task_file_name> -c <config_file_name>
- o <output_folder>

IMPORTANT: this function is called by the main routine in database.py
so you should never have to call it manually
--------------
Daniel Wolfensberger, LTE-MeteoSwiss, 2020
Rebecca Gugerli, LTE-MeteoSwiss, 2022
"""

import numpy as np
import pandas as pd
import datetime
import logging
import dask.dataframe as dd

logging.basicConfig(level=logging.INFO)
import os

from collections import OrderedDict
from optparse import OptionParser

from rainforest.common import constants
from rainforest.common.lookup import get_lookup
from rainforest.common.utils import read_task_file, envyaml, nested_dict_values
from rainforest.common.retrieve_data import retrieve_prod, retrieve_CPCCV, retrieve_AQC_XLS
from rainforest.common.io_data import read_cart

try:
    import pysteps
    _PYSTEPS_AVAILABLE = True
except ImportError:
    _PYSTEPS_AVAILABLE = False

IGNORE_ERRORS = True

class Updater(object):
    def __init__(self, task_file, config_file, output_folder, debug = False):
        """
        Creates an Updater  class instance that allows to add new reference
        data to the database
        
        Parameters
        ----------
        task_file : str
            The full path to a task file, i.e. a file with the following format
            timestamp, station1, station2, station3...stationN
            These files are generated by the database.py module so normally you
            shouldn't have to create them yourself
        config_file : str
            The full path of a configuration file written in yaml format
            that indicates how the radar retrieval must be done
        output_folder: str
            The full path where the generated files will be stored
        debug: bool
            If set to true will not except any error in the code
        """

        if debug:
            global IGNORE_ERRORS
            IGNORE_ERRORS = False

        self.config = envyaml(config_file)
        self.tasks = read_task_file(task_file)
        self.output_folder = output_folder
        self.downloaded_files = [] # Keeps track of all downloaded files
        
        self.ref_config = self.config['REFERENCE_RETRIEVAL']
        self.neighb_x = self.ref_config['NEIGHBOURS_X']
        self.neighb_y = self.ref_config['NEIGHBOURS_Y']
        self.products = self.ref_config['PRODUCTS']
        # Decompose motion vectors variables
        products_decomposed = []
        for prod in self.products:
            if 'MV' in prod:
                if not _PYSTEPS_AVAILABLE:
                    logging.error("Pysteps is not available, product {:s} will not be extracted!")
                    continue
                products_decomposed.append(prod + '_x')
                products_decomposed.append(prod + '_y')
            else:
                products_decomposed.append(prod)
        self.products = products_decomposed
        self.dims = {'np': len(self.products),
                     'nnx': len(self.neighb_x),
                     'nny': len(self.neighb_y)}
        
    def retrieve_cart_files(self, start_time, end_time, products):
        """
        Retrieves a set of reference product files for a given time range
        
        Parameters
        ----------
        start_time : datetime.datetime instance
            starting time of the time range
        end_time : datetime.datetime instance
            end time of the time range
        products : list of str
            list of all products to retrieve, must be valid MeteoSwiss product
            names, for example CPC, CPCH, RZC, MZC, BZC, etc
        """
        
        files_allproducts = {}

        for prod in products:
            try:
                if prod == 'CPC' or prod == 'CPCH':
                    files = retrieve_prod(self.config['TMP_FOLDER'],
                                                 start_time, end_time, prod, 
                                                 pattern = '*5.801.gif')
                else:
                    files = retrieve_prod(self.config['TMP_FOLDER'],
                                                 start_time, end_time, prod)
              
                files_allproducts[prod] = files
                self.downloaded_files.extend(nested_dict_values(files_allproducts))
            except:
                logging.error("""Retrieval for product {:s} at timesteps {:s}-{:s} 
                          failed""".format(prod, str(start_time), 
                                                 str(end_time)))
                files_allproducts[prod] = []
                if not IGNORE_ERRORS:
                    raise
                
        return files_allproducts

    def process_all_timesteps(self):
        """
        Processes all timestaps in the task file
        """
        
        # Get relevant parameters from user config
        fill_value = self.config['NO_DATA_FILL']
        nneighb = self.dims['nnx'] * self.dims['nny']
        logging.info('Products: '+','.join(self.products))
        logging.info('Nx      : '+','.join([str(n) for n in self.neighb_x]))
        logging.info('Ny      : '+','.join([str(n) for n in self.neighb_y]))
             

        # All 10 min timestamps to process (gauge data)
        all_timesteps = list(self.tasks.keys())
        
        # LUT to get cartesian data at gauge
        lut_cart = get_lookup('station_to_qpegrid')
        
        # Initialize output
        data_10minagg = [] # Contains all 10 min data for all products
        data_cst = [] # for time, sta, nx,ny
        
        if 'CPC.CV' in self.products:
            data_cpccv = [] # separate list for cpccv
            data_cst_cpccv = [] # for time, sta, nx,ny
            include_cpccv = True
            self.products.remove('CPC.CV')
            colnames_cpccv = ['TIMESTAMP','STATION','NX','NY']
            colnames_cpccv.append('CPC.CV')
            # If getting CPC.CV, also get CPC from excel files
            try:
                self.products.remove('CPC_XLS')
            except:
                pass
            data_cpcxls = []
            colnames_cpccv.append('CPC_XLS')
        else:
            include_cpccv = False

        if 'AQC_XLS' in self.products:
            data_aqcxls = []
            data_cst_aqcxls = []
            include_aqcxls = True
            self.products.remove('AQC_XLS')
            colnames_aqcxls = ['TIMESTAMP','STATION','NX','NY']
            colnames_aqcxls.append('AQC_XLS')
        else:
            include_aqcxls = False

        # Initiate
        current_hour = None
         
        # For motion vectors
        oflow_method = pysteps.motion.get_method(self.ref_config['MV_METHOD'])
        
        colnames = ['TIMESTAMP','STATION','NX','NY']
        colnames.extend(self.products)
        
        for i, tstep in enumerate(all_timesteps):
            logging.info('Processing timestep '+str(tstep))
            
            # Set t-start -5 minutes to get all the files between, e.g., H:01 and H:10 and log at H:10
            tstart = datetime.datetime.utcfromtimestamp(float(tstep)) - datetime.timedelta(minutes=8)
            tend= datetime.datetime.utcfromtimestamp(float(tstep))
            
            stations_to_get = self.tasks[tstep]
            
            hour_of_year = datetime.datetime.strftime(tend,'%Y%m%d%H')
            day_of_year = hour_of_year[0:-2]
            
            current_day = day_of_year
                
            # Get CPC.CV data
            if hour_of_year != current_hour:
                current_hour = hour_of_year
                if include_cpccv:
                    logging.info('Retrieving product CPC.CV and CPC_XLS for hour {}'.format(current_hour))

                    # CPC.CV only contains a measurement every full hour
                    tstep_xls = datetime.datetime.strptime(current_hour,'%Y%m%d%H')

                    data_at_stations, data_at_stations_cpc = retrieve_CPCCV(tstep_xls, stations_to_get)
                    
                    # Replace NAN
                    data_at_stations[np.isnan(data_at_stations)] = fill_value
                    data_at_stations_cpc[np.isnan(data_at_stations_cpc)] = fill_value

                    # Assign CPC.CV values to rows corresponding to nx = ny = 0
                    data_cpccv.extend(data_at_stations)
                    data_cpcxls.extend(data_at_stations_cpc)

                    # CPC.CV only contains a measurement every full hour
                    tstep_xls = int(tstep_xls.replace(tzinfo=
                                datetime.timezone.utc).timestamp())
                    for sta in stations_to_get:
                        data_cst_cpccv.append([tstep_xls,sta,0,0]) # nx = ny = 0

                if include_aqcxls:
                    logging.info('Retrieving product AQC from Excel file for hour {}'.format(current_hour))

                    # AQC_XLS only contains a measurement every full hour
                    tstep_xls = datetime.datetime.strptime(current_hour,'%Y%m%d%H')

                    # Get value
                    data_at_stations = retrieve_AQC_XLS(tstep_xls, stations_to_get)
                    
                    # Replace NAN
                    data_at_stations[np.isnan(data_at_stations)] = fill_value

                    # Assign CPC.CV values to rows corresponding to nx = ny = 0
                    data_aqcxls.extend(data_at_stations)

                    # AQC_XLS only contains a measurement every full hour
                    tstep_xls = int(tstep_xls.replace(tzinfo=
                                datetime.timezone.utc).timestamp())
                    for sta in stations_to_get:
                        data_cst_aqcxls.append([tstep_xls,sta,0,0]) # nx = ny = 0

            # Initialize output
            N,M = len(stations_to_get) * nneighb, self.dims['np']
            data_allprod = np.zeros((N,M), dtype = np.float32) + np.nan
            
            # Get data
            baseproducts = [prod for prod in self.products if 'MV' not in prod]
            allfiles = self.retrieve_cart_files(tstart, tend, baseproducts)
            
            for j, prod in enumerate(self.products):
                logging.info('Retrieving product ' + prod)
                if 'MV' in prod:
                    if '_x' in prod:
                        idx_slice_mv = 0
                        # Motion vector case
                        ###################
                        # Get product for which to compute MV
                        baseprod = prod.strip('MV').split('_')[0]
                        # Initialize output
                        N = len(stations_to_get) * nneighb
                        data_prod = np.zeros((N,), dtype = np.float32) + np.nan
                        
                        try:
                            # For CPC we take only gif
                            files  = allfiles[baseprod]
                            
                            R = []
                            for f in files:
                                R.append(read_cart(f))
                            R = np.array(R)
                            R[R<0] = np.nan
                            mv = oflow_method(R)
                            
                            # Mask mv where there is no rain
                            mask = np.nansum(R, axis = 0) <= 0
                            mv[:,mask] = 0
                        except:
                            # fill with missing values, we don't care about the exact dimension
                            mv = np.zeros((2,1000,1000)) + fill_value 
                            if not IGNORE_ERRORS:
                                raise

                    elif '_y' in prod: # mv already computed
                        idx_slice_mv = 1 
                        
                    idx_row = 0 # Keeps track of the row
                    for sta in stations_to_get: # Loop on stations
                        for nx in self.neighb_x:
                            for ny in self.neighb_y:
                                strnb = '{:d}{:d}'.format(nx,ny)
                                # Get idx of Cart pixel in 2D map
                                idx = lut_cart[sta][strnb]
                                data_prod[idx_row] = mv[idx_slice_mv, idx[0],idx[1]]
                                idx_row += 1

                else:
                    # Normal product case
                    ###################
                    files = allfiles[prod]
                    
                    # # Initialize output
                    N,M = len(stations_to_get) * nneighb, len(files)
                    data_prod = np.zeros((N,M), dtype = np.float32) + np.nan
                    
                    
                    for k, f in enumerate(files):
                        try:
                            proddata = read_cart(f)
                        except:
                            # fill with missing values, we don't care about the exact dimension
                            proddata = np.zeros((1000,1000)) + np.nan
                            
                        # Threshold radar precip product
                        if prod == 'RZC' or prod == 'AQC':
                            proddata[proddata < constants.MIN_RZC_VALID] = 0
                            
                        idx_row = 0 # Keeps track of the row
                        for sta in stations_to_get: # Loop on stations
                            for nx in self.neighb_x:
                                for ny in self.neighb_y:
                                    strnb = '{:d}{:d}'.format(nx,ny)
                                    # Get idx of Cart pixel in 2D map
                                    idx = lut_cart[sta][strnb]
                                    data_prod[idx_row,k] = proddata[idx[0],idx[1]]
                                    # Add next row
                                    idx_row += 1
                                                
                    data_prod = np.nanmean(data_prod,axis = 1)
                    data_prod[np.isnan(data_prod)] = fill_value
                    
                data_allprod[:,j] = data_prod
            
            for prod in allfiles.keys():
                for f in allfiles[prod]:
                    try:
                        os.remove(f)
                    except:
                        pass
                    
            data_10minagg.extend(data_allprod)
    
            # Add constant data
            for sta in stations_to_get:
                for nx in self.neighb_x:
                    for ny in self.neighb_y:
                        data_cst.append([tstep,sta,nx,ny])

            # Save data to file if end of loop or new day
            if (i == len(all_timesteps) - 1):
                save_output = True
            else: 
                next_tstep = datetime.datetime.utcfromtimestamp(all_timesteps[i+1])
                next_tstep_day = datetime.datetime.strftime(next_tstep,'%Y%m%d%H')[0:-2]
                if current_day != next_tstep_day:
                    save_output = True
                else:
                    save_output = False
     
            if save_output and len(data_cst):
                logging.info('Saving new table for day {:s}'.format(str(current_day)))
                name = self.output_folder + current_day + '.parquet'

                # Check if a file already exists
                file_exists = False
                if os.path.exists(name):
                    file_exists = True   

                data_10minagg = np.array(data_10minagg)
                data_cst = np.array(data_cst)
                # Concatenate metadata and product data
                all_data = np.hstack((data_cst, data_10minagg))
                dic = OrderedDict()
                
                for c, col in enumerate(colnames):
                    data_col = all_data[:,c]
                    isin_listcols = [col in c for c in constants.COL_TYPES.keys()]
                    if any(isin_listcols):
                        idx = np.where(isin_listcols)[0][0]
                        coltype = list(constants.COL_TYPES.values())[idx]
                        try:
                            data_col = data_col.astype(coltype)
                        except:# for int
                            data_col = data_col.astype(float).astype(coltype)
                            if not IGNORE_ERRORS:
                                raise
                    else:
                        data_col = data_col.astype(np.float32)
                    dic[col] = data_col
             
                df = pd.DataFrame(dic)
       
                if include_cpccv:
                    data_cst_cpccv = np.array(data_cst_cpccv)
                    data_cpccv = np.array([data_cpccv]).T
                    data_cpcxls = np.array([data_cpcxls]).T
                    all_data_cpccv = np.hstack((data_cst_cpccv, data_cpccv, data_cpcxls))
                    
                    dic_cpccv = OrderedDict()
                    for c, col in enumerate(colnames_cpccv):
                        data_col = all_data_cpccv[:,c]
                        isin_listcols = [col in c for c 
                                             in constants.COL_TYPES.keys()]
                        if any(isin_listcols):
                            idx = np.where(isin_listcols)[0][0]
                            coltype = list(constants.COL_TYPES.values())[idx]
                            try:
                                data_col = data_col.astype(coltype)
                            except:# for int
                                data_col = data_col.astype(float).astype(coltype)
                                if not IGNORE_ERRORS:
                                    raise
                        else:
                            data_col = data_col.astype(np.float32)
                        dic_cpccv[col] = data_col
                    
                    dfcpc = pd.DataFrame(dic_cpccv)
                    df = pd.merge(df, dfcpc, 
                                  on = ['STATION','TIMESTAMP','NX','NY'],
                                  how = 'left')
                    df.replace(np.nan,fill_value)

                # Add AQC Values
                if include_aqcxls:
                    data_cst_aqcxls = np.array(data_cst_aqcxls)
                    data_aqcxls = np.array([data_aqcxls]).T
                    all_data_aqcxls = np.hstack((data_cst_aqcxls, data_aqcxls))
                    
                    dic_aqcxls = OrderedDict()
                    for c, col in enumerate(colnames_aqcxls):
                        data_col = all_data_aqcxls[:,c]
                        isin_listcols = [col in c for c 
                                             in constants.COL_TYPES.keys()]
                        if any(isin_listcols):
                            idx = np.where(isin_listcols)[0][0]
                            coltype = list(constants.COL_TYPES.values())[idx]
                            try:
                                data_col = data_col.astype(coltype)
                            except:# for int
                                data_col = data_col.astype(float).astype(coltype)
                                if not IGNORE_ERRORS:
                                    raise
                        else:
                            data_col = data_col.astype(np.float32)
                        dic_aqcxls[col] = data_col
                    
                    dfcpc = pd.DataFrame(dic_aqcxls)
                    df = pd.merge(df, dfcpc, 
                                  on = ['STATION','TIMESTAMP','NX','NY'],
                                  how = 'left')
                    df.replace(np.nan,fill_value)

                if file_exists == False:
                    logging.info('Saving file ' + name)
                    df.to_parquet(name, compression = 'gzip', index = False)
                else:
                    logging.info('Saving file: '+name+' as it already exists, I will append to it')
                    # Rename old file to be able to delete it afterwards
                    name_old = name[0:-8] + '_old'+ name[-8::]
                    os.rename(name, name_old)
                    # Read dask DataFrame and convert it to Pandas DataFrame
                    df_old = dd.read_parquet(name_old).compute()
                    # Merge the old and new one and drop duplicate rows
                    df_join = df_old.append(df).drop_duplicates()
                    # Save the new file and delete the old one
                    df_join.to_parquet(name, compression = 'gzip', index = False)
                    os.remove(name_old)
                
                # Reset lists                  
                data_10minagg = [] # separate list for cpccv
                data_cst = [] # for time, sta, nx,ny
                if include_cpccv:
                    data_cst_cpccv = [] # for time, sta, nx,ny 
                    data_cpccv = [] # separate list for cpccv
                    data_cpcxls = [] # separate list for cpc from xls files
                if include_aqcxls:
                    data_cst_aqcxls = []
                    data_aqcxls = []
                    

    def final_cleanup(self):
        """
        Performs a final cleanup by checking if all files in downloaded_files 
        deleted       
        """  
        for f in self.downloaded_files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except PermissionError as e:
                    logging.error(e)
                    logging.error('Could not delete file {:s}'.format(f))

if __name__ == '__main__':
    parser = OptionParser()
    
    parser.add_option("-c", "--configfile", dest = "config_file",
                      help="Specify the user configuration file to use",
                      metavar="CONFIG")
    
    
    parser.add_option("-t", "--taskfile", dest = "task_file", default = None,
                      help="Specify the task file to process", metavar="TASK")
    
    parser.add_option("-o", "--output", dest = "output_folder", default = '/tmp/',
                      help="Specify the output directory", metavar="FOLDER")
    
    (options, args) = parser.parse_args()
    
    
    u = Updater(options.task_file, options.config_file, options.output_folder)
    u.process_all_timesteps()
    u.final_cleanup()
