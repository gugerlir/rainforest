#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Main module to 
"""

# Global imports
import os
import pickle
import glob
import dask.dataframe as dd
import pandas as pd
import numpy as np
import datetime
from pathlib import Path
from scipy.stats import rankdata

# Local imports
from ..common import constants
from ..ml.utils import vert_aggregation, split_event, split_years
from ..ml.rfdefinitions import RandomForestRegressorBC, QuantileRandomForestRegressorBC, read_rf
from ..common.utils import perfscores, envyaml
from ..common.graphics import plot_crossval_stats
from ..common.logger import logger


dir_path = os.path.dirname(os.path.realpath(__file__))
FOLDER_MODELS = Path(os.environ['RAINFOREST_DATAPATH'], 'rf_models')

def readInputData(input_location, tstart, tend, datalist=['gauge', 'radar', 'refer']):
    
    if 'gauge' in datalist:
        gaugetab = pd.read_parquet(str(Path(input_location, 'gauge.parquet')))
    if 'radar' in datalist:
        radartab = pd.read_parquet(str(Path(input_location, 'radar_x0y0.parquet')))
    if 'refer' in datalist:
        refertab = pd.read_parquet(str(Path(input_location, 'reference_x0y0.parquet')))
    
    grp = pickle.load(open(str(Path(input_location, 'grouping_idx_x0y0.p')),'rb'))
    grp_hourly = grp['grp_hourly']; grp_vertical = grp['grp_vertical']

    if tstart != None:
        try:
            tstart = datetime.datetime.strptime(tstart,
                    '%Y%m%d%H%M').replace(tzinfo=datetime.timezone.utc).timestamp()
        except:
            tstart = gaugetab['TIMESTAMP'].min()
            logger.info('The format of tstart was wrong, taking the earliest date')
    if tend != None:
        try:
            tend = datetime.datetime.strptime(tend,
                    '%Y%m%d%H%M').replace(tzinfo=datetime.timezone.utc).timestamp()
        except:
            tend = gaugetab['TIMESTAMP'].max()
            logger.info('The format of tend was wrong, taking the earliest date')

    timevalid = gaugetab['TIMESTAMP'].copy().astype(bool)
    vertvalid = radartab['TIMESTAMP'].copy().astype(bool)

    if (tstart != None):
        timevalid[(gaugetab['TIMESTAMP'] < tstart)] = False
        vertvalid[(radartab['TIMESTAMP'] < tstart)] = False
    if (tend != None):
        timevalid[(gaugetab['TIMESTAMP'] > tend)] = False
        vertvalid[(radartab['TIMESTAMP'] > tend)] = False

    gaugetab = gaugetab[timevalid]
    grp_hourly = grp_hourly[timevalid]
    radartab = radartab[vertvalid]
    grp_vertical = grp_vertical[vertvalid]    

    if 'refer' in datalist:
        refertab = refertab[timevalid]
        return gaugetab, radartab, refertab, grp_hourly, grp_vertical
    else:
        return gaugetab, radartab, grp_hourly, grp_vertical

def processFeatures(features_dic, radartab):
    # currently the only supported additional features is zh (refl in linear units)
    # and DIST_TO_RAD{A-D-L-W-P} (dist to individual radars)
    # Get list of unique features names
    if type(features_dic) == dict :
        features = np.unique([item for sub in list(features_dic.values())
                            for item in sub])
    else:
        features = features_dic
        
    for f in features:
        if 'zh' in f:
            logger.info('Converting reflectivity {:s} from log [dBZ] to linear [mm^6 m^-3]'.format(f))
            try:
                radartab[str(f)] = 10**(0.1 * radartab[f.replace('zh','ZH')+'_mean'].copy())
            except:
                radartab[str(f)] = 10**(0.1 * radartab[f.replace('zh','ZH')].copy())                        
        elif 'zv' in f:
            logger.info('Computing derived variable {:s}'.format(f))
            try:
                radartab[str(f)] = 10**(0.1 * radartab[f.replace('zv','ZV')+'_mean'].copy())
            except:
                radartab[str(f)] = 10**(0.1 * radartab[f.replace('zv','ZV')].copy())      
        if 'DIST_TO_RAD' in f:
            info_radar = constants.RADARS
            vals = np.unique(radartab['RADAR'])
            for val in vals:
                dist = np.sqrt((radartab['X'] - info_radar['X'][val])**2+
                        (radartab['Y'] - info_radar['Y'][val])**2) / 1000.
                radartab['DIST_TO_RAD' + str(val)] = dist

    features = [str(f) for f in features]

    return radartab, features

def getTempIDX(TempOBS, grp_hourly, test, train) : 

    # Get reference values
    T_test_60 = np.squeeze(np.array(pd.DataFrame(TempOBS[test])
                    .groupby(grp_hourly[test]).mean()))
    
    T_train_60 = np.squeeze(np.array(pd.DataFrame(TempOBS[train])
                    .groupby(grp_hourly[train]).mean()))
    
    # Dictionnary with all indices:
    IDX = {}

    for tagg in ['10min', '60min']:
        IDX[tagg] = {}
        for data_type in ['test', 'train']:
            IDX[tagg][data_type] = {}
            for precip_type in ['all', 'liquid', 'solid']:
                IDX[tagg][data_type][precip_type] = {}

    IDX['10min']['train']['liquid'] = TempOBS[train] >= constants.THRESHOLD_SOLID
    IDX['10min']['train']['solid'] = TempOBS[train] < constants.THRESHOLD_SOLID

    IDX['60min']['train']['liquid'] = T_train_60 >= constants.THRESHOLD_SOLID
    IDX['60min']['train']['solid'] = T_train_60 < constants.THRESHOLD_SOLID

    IDX['10min']['test']['liquid'] = TempOBS[test] >= constants.THRESHOLD_SOLID
    IDX['10min']['test']['solid'] = TempOBS[test] < constants.THRESHOLD_SOLID

    IDX['60min']['test']['liquid'] = T_test_60 >= constants.THRESHOLD_SOLID
    IDX['60min']['test']['solid'] = T_test_60 < constants.THRESHOLD_SOLID

    return IDX


def prepareScoreDic(modelnames, station_scores=True):

    all_scores = {'10min':{},'60min':{}}
    all_stats = {'10min':{},'60min':{}}
    
    if station_scores == True:
        all_station_scores = {'10min': {}, '60min': {}}
        all_station_stats = {'10min': {}, '60min': {}}

    for tagg in ['10min', '60min']:
        for model in modelnames:
            all_scores[tagg][model] = {'train': {'solid':[],'liquid':[],'all':[]},
                                    'test': {'solid':[],'liquid':[],'all':[]}}
            all_stats[tagg][model] = {'train': {'solid':{},'liquid':{},'all':{}},
                                    'test': {'solid':{},'liquid':{},'all':{}}}
            if station_scores == True:
                all_station_scores[tagg][model] = {'solid':{},'liquid':{},'all':{}}
                all_station_stats[tagg][model] = {'solid':{},'liquid':{},'all':{}}

    if station_scores == False:
        return all_scores, all_stats
    else:
        return all_scores, all_stats, all_station_scores, all_station_stats

def calcScore(all_scores, obs, pred, idxTemp, bounds, 
                tagg = '10min', model='RFO', data_type='test'):

    for pp_type in all_scores[tagg][model][data_type].keys():
        idx = idxTemp[tagg][data_type][pp_type]
        scores = perfscores(pred[idx], obs[idx], bounds=bounds)
        all_scores[tagg][model][data_type][pp_type].append(scores)

    return all_scores


class RFTraining(object):
    '''
    This is the main class that allows to preparate data for random forest
    training, train random forests and perform cross-validation of trained models
    '''
    def __init__(self, db_location, input_location=None,
                 force_regenerate_input = False):
        """
        Initializes the class and if needed prepare input data for the training
        
        Note that when calling this constructor the input data is only 
        generated for the central pixel (NX = NY = 0 = loc of gauge), if you
        want to regenerate the inputs for all neighbour pixels, please 
        call the function self.prepare_input(only_center_pixel = False)
        
        Parameters
        ----------
        db_location : str
            Location of the main directory of the database (with subfolders
            'reference', 'gauge' and 'radar' on the filesystem)
        input_location : str
            Location of the prepared input data, if this data cannot be found
            in this folder, it will be computed here, default is a subfolder
            called rf_input_data within db_location
        force_regenerate_input : bool
            if True the input parquet files will always be regenerated from
            the database even if already present in the input_location folder
        """
        
        if input_location == None:
            input_location = str(Path(db_location, 'rf_input_data'))
            
        # Check if at least gauge.parquet, refer_x0y0.parquet and radar_x0y0.parquet
        # are present
        valid = True
        if not os.path.exists(input_location):
            valid = False
            os.makedirs(input_location)
        files = glob.glob(str(Path(input_location, '*')))
        files = [os.path.basename(f) for f in files]
        if ('gauge.parquet' not in files or 'reference_x0y0.parquet' not in files
            or 'radar_x0y0.parquet' not in files):
            valid = False
        
        self.input_location = input_location
        self.db_location = db_location
        
        if not valid :
            logger.info('Could not find valid input data from the folder {:s}'.format(input_location))
        # if force_regenerate_input or not valid:
        #     logger.info('The program will now compute this input data from the database, this takes quite some time')
        #     self.prepare_input()
    
    def prepare_input(self, only_center=True, foldername_radar='radar'):
        """
        Reads the data from the database  in db_location and processes it to 
        create easy to use parquet input files for the ML training and stores 
        them in the input_location, the processing steps involve
        
        For every neighbour of the station (i.e. from -1-1 to +1+1):
        
        -   Replace missing flags by nans
        -   Filter out timesteps which are not present in the three tables 
            (gauge, reference and radar)
        -   Filter out incomplete hours (i.e. where less than 6 10 min timesteps
            are available)
        -   Add height above ground and height of iso0 to radar data
        -   Save a separate parquet file for radar, gauge and reference data
        -   Save a grouping_idx pickle file containing *grp_vertical*
            index (groups all radar rows with same timestep and station),
            *grp_hourly* (groups all timesteps with same hours) and *tstamp_unique*
            (list of all unique timestamps)
        
        Parameters
        ----------
        only_center : bool
            If set to True only the input data for the central neighbour
            i.e. NX = NY = 0 (the location of the gauge) will be recomputed
            this takes much less time and is the default option since until
            now the neighbour values are not used in the training of the RF
            QPE
        foldername_radar: str
            Name of the folder to use for the radar data. Default name is 'radar'            
        """
        
        if not os.path.exists(Path(self.db_location, foldername_radar)):
            logger.error('Invalid foldername for radar data, please check')

        if only_center:
            nx = [0]
            ny = [0]
        else:
            nx = [0,1,-1]
            ny = [0,1,-1]
        gauge = dd.read_csv(str(Path(self.db_location, 'gauge', '*.csv.gz')), 
                            compression='gzip', 
                            assume_missing=True,
                            dtype = {'TIMESTAMP':int,  'STATION': str})
        
        gauge = gauge.compute().drop_duplicates()
        gauge = gauge.replace(-9999,np.nan)
        for x in nx:
            for y in ny:
                logger.info('Processing neighbour {:d}{:d}'.format(x, y))
                radar = dd.read_parquet(str(Path(self.db_location, foldername_radar,
                                                  '*.parquet')))
                refer = dd.read_parquet(str(Path(self.db_location, 'reference', 
                                                 '*.parquet')))
                        
                # Select only required pixel
                radar = radar.loc[np.logical_and(radar['NX'] == x, 
                                                  radar['NY'] == y)]
                refer = refer.loc[np.logical_and(refer['NX'] == x, 
                                                 refer['NY'] == y)]
                
                # Convert to pandas and remove duplicates 
                radar = radar.compute().drop_duplicates(subset = ['TIMESTAMP',
                                                                   'STATION',
                                                                   'RADAR',
                                                                   'NX','NY',
                                                                   'SWEEP'])
                
                refer = refer.compute().drop_duplicates(subset = ['TIMESTAMP',
                                                                  'STATION'])
                
                # Replace missing flags with nan
                radar = radar.replace(-9999, np.nan)
                refer = refer.replace(-9999, np.nan)

                # Sort values
                radar = radar.sort_values(by = ['TIMESTAMP','STATION','SWEEP'])
                refer = refer.sort_values(by = ['TIMESTAMP','STATION'])
                gauge = gauge.sort_values(by = ['TIMESTAMP','STATION'])

                # Get only valid precip data
                gauge = gauge[np.isfinite(gauge['RRE150Z0'])]
                
                # Create individual 10 min - station stamps
                gauge['s-tstamp'] = np.array(gauge['STATION'] + 
                                           gauge['TIMESTAMP'].astype(str)).astype(str)
                radar['s-tstamp'] = np.array(radar['STATION'] + 
                                            radar['TIMESTAMP'].astype(str)).astype(str)
                refer['s-tstamp'] = np.array(refer['STATION'] + 
                                           refer['TIMESTAMP'].astype(str)).astype(str)
                
                # Get gauge and reference only when radar data available
        
                # Find timestamps that are in the three datasets
                ststamp_common = np.array(pd.Series(list(set(gauge['s-tstamp'])
                                    .intersection(set(refer['s-tstamp'])))))
                ststamp_common = np.array(pd.Series(list(set(radar['s-tstamp'])
                                     .intersection(set(ststamp_common)))))
                radar = radar.loc[radar['s-tstamp'].isin(ststamp_common)]
                gauge = gauge.loc[gauge['s-tstamp'].isin(ststamp_common)]
                refer = refer.loc[refer['s-tstamp'].isin(ststamp_common)]
        
                # Filter incomplete hours
                stahour = np.array(gauge['STATION'] + 
                       ((gauge['TIMESTAMP'] - 600 ) - 
                         (gauge['TIMESTAMP'] - 600 ) % 3600).astype(str)).astype(str)
                  
                full_hours = np.array(gauge.groupby(stahour)['STATION']
                                        .transform('count') == 6)
               
                refer = refer[full_hours]
                gauge = gauge[full_hours]    
                radar = radar[radar['s-tstamp'].
                                isin(np.array(gauge['s-tstamp']))]
                
                stahour = stahour[full_hours]
                
                # Creating vertical grouping index
                
                _, idx, grp_vertical = np.unique(radar['s-tstamp'],
                                                 return_inverse = True,
                                                 return_index = True)
                # Get original order
                sta_tstamp_unique = radar['s-tstamp'].index[np.sort(idx)]
                # Preserves order and avoids sorting radar_statstamp
                grp_vertical = idx[grp_vertical]
                # However one issue is that the indexes are not starting from zero with increment
                # of one, though they are sorted, they are like 0,7,7,7,15,15,23,23
                # We want them starting from zero with step of one
                grp_vertical = rankdata(grp_vertical,method='dense') - 1
                
                # Repeat operation with gauge hours
                sta_hourly_unique, idx, grp_hourly = np.unique(stahour, 
                                                           return_inverse = True,
                                                           return_index = True)
                grp_hourly = idx[grp_hourly]
                
                # Add derived variables  height iso0 (HISO) and height above ground (HAG)
                # Radar
                stations = constants.METSTATIONS
                cols = list(stations.columns)
                cols[1] = 'STATION'
                stations.columns = cols
                radar = pd.merge(radar,stations, how = 'left', on = 'STATION',
                                 sort = False)
                
                if 'T' in radar.columns:
                    radar['HISO'] = -radar['T'] / constants.LAPSE_RATE * 100
                    radar['HAG'] = radar['HEIGHT'] - radar['Z']
                    radar['HAG'][radar['HAG'] < 0] = 0
        
                # Gauge
                gauge['minutes'] = (gauge['TIMESTAMP'] % 3600)/60
                
                # Save all to file
                # Save all to file
                logger.info('Saving files to {}'.format(self.input_location))
                refer.to_parquet(str(Path(self.input_location, 
                                          'reference_x{:d}y{:d}.parquet'.format(x,y))),
                                 compression = 'gzip', index = False)
                
                radar.to_parquet(str(Path(self.input_location, 
                                          'radar_x{:d}y{:d}.parquet'.format(x,y))),
                                 compression = 'gzip', index = False)
                
                grp_idx = {}
                grp_idx['grp_vertical'] = grp_vertical
                grp_idx['grp_hourly'] = grp_hourly
                grp_idx['tstamp_unique'] = sta_tstamp_unique
                
                pickle.dump(grp_idx, 
                    open(str(Path(self.input_location, 
                                  'grouping_idx_x{:d}y{:d}.p'.format(x,y))),'wb'))
                
                if x == 0 and y == 0:
                    # Save only gauge for center pixel since it's available only there
                    gauge.to_parquet(str(Path(self.input_location, 'gauge.parquet')),
                                 compression = 'gzip', index = False)
        
    def prepare_input_vert_agg(self, aggregation_params, features=None, 
                        tstart = None, tend = None,
                        output_folder = None):
        """
            Calculates the vertical aggregation of the input features dic and saves
            it to a file to run tests faster

        Parameters
        ----------
        aggregation_params : dict
            Dict with two keywords: beta and visib_weighting 
            If not given, the default is used: {'beta' : -0.5 , 'visib_weighting: True}
        features : list
            A list with features that will be aggregated to the ground,
            e.g., ['RADAR', 'zh_VISIB_mean',
            'zv_VISIB_mean','KDP_mean','RHOHV_mean','T', 'HEIGHT','VISIB_mean']}
        tstart : str
            the starting time of the training time interval, default is to start
            at the beginning of the time interval covered by the database
        tend : str
            the end time of the training time interval, default is to end
            at the end of the time interval covered by the database   
        output_folder : str
            Location where to store the trained models in pickle format,
            if not provided it will store them in the standard location 
            str(Path(db_location, 'rf_input_data'))
        """
        
        if output_folder == None:
            output_folder =  self.input_location
            
        if aggregation_params == None:
            vert_agg_params = {'beta' : -0.5, 'visib_weighting' : 1}
        else:
            vert_agg_params = aggregation_params
  
        ###############################################################################
        # Read and filter data
        ###############################################################################
        _, radartab, _, grp_vertical = \
            readInputData(self.input_location, tstart, tend, 
                        datalist=['gauge', 'radar'])

        if features == None:
            features = radartab.columns

        ###############################################################################
        # Compute vertical aggregation and initialize model
        ###############################################################################
        radartab, features = processFeatures(features, radartab)
                    
        ###############################################################################
        # Compute data filter for each model
        ###############################################################################
        beta = vert_agg_params['beta']
        visib_weigh = vert_agg_params['visib_weighting']

        vweights = 10**(beta * (radartab['HEIGHT']/1000.)) # vert. weights

        ###############################################################################
        # Prepare training dataset
        ###############################################################################        
        logger.info('Performing vertical aggregation of input features')                
        features_VERT_AGG = vert_aggregation(radartab[features], 
                                vweights, grp_vertical,visib_weigh,
                                radartab['VISIB_mean'])
                    
        ###############################################################################
        # Fit
        ###############################################################################
        # create name of variables used in the model
        features = []
        for f in features_VERT_AGG.columns:
            if '_max' in f:
                f = f.replace('_max','')
            elif '_min' in f:
                f = f.replace('_min','')
            elif '_mean' in f:
                f = f.replace('_mean','')
            features.append(f)

        features_VERT_AGG['TIMESTAMP'] = radartab['TIMESTAMP'].groupby(grp_vertical).first()
        features_VERT_AGG['STATION'] = radartab['STATION'].groupby(grp_vertical).first()

        features_VERT_AGG.to_parquet(str(Path(self.input_location, 
                'feat_vert_agg_BETA_{:1.1f}_VisibWeigh_{:d}.parquet'.format(beta,visib_weigh))),
                compression = 'gzip', index = False)

        filename = str(Path(self.input_location, \
                    'feat_vert_agg_BETA_{:1.1f}_VisibWeigh_{:d}_README.txt'.\
                    format(beta,visib_weigh)))
        with open(filename, 'w') as f:
                f.write('Colnames\n'+ (';').join(list(features_VERT_AGG.columns)) + '\n')
                f.write('Names in model\n' + (';').join(features) + '\n')


    def fit_models(self, config_file, features_dic, tstart = None, tend = None,
                   output_folder = None):
        """
        Fits a new RF model that can be used to compute QPE realizations and
        saves them to disk in pickle format
        
        Parameters
        ----------
        config_file : str
            Location of the RF training configuration file, if not provided 
            the default one in the ml submodule will be used       
        features_dic : dict
            A dictionary whose keys are the names of the models you want to
            create (a string) and the values are lists of features you want to
            use. For example {'RF_dualpol':['RADAR', 'zh_VISIB_mean',
            'zv_VISIB_mean','KDP_mean','RHOHV_mean','T', 'HEIGHT','VISIB_mean']}
            will train a model with all these features that will then be stored
            under the name RF_dualpol_BC_<type of BC>.p in the ml/rf_models dir
        tstart : str
            the starting time of the training time interval, default is to start
            at the beginning of the time interval covered by the database
        tend : str
            the end time of the training time interval, default is to end
            at the end of the time interval covered by the database   
        output_folder : str
            Location where to store the trained models in pickle format,
            if not provided it will store them in the standard location 
            <library_path>/ml/rf_models
        """
        
        if output_folder == None:
            output_folder =  str(Path(FOLDER_MODELS, 'rf_models'))
            
        try:
            config = envyaml(config_file)
        except:
            logger.warning('Using default config as no valid config file was provided')
            config_file = dir_path + '/default_config.yml'
            
        config = envyaml(config_file)
  
        ###############################################################################
        # Read and filter data
        ###############################################################################
        gaugetab, radartab, _, grp_vertical = \
            readInputData(self.input_location, tstart, tend, 
                        datalist=['gauge', 'radar'])

        ###############################################################################
        # Compute vertical aggregation and initialize model
        ###############################################################################
        radartab, features = processFeatures(features_dic, radartab)
                    
        ###############################################################################
        # Compute data filter for each model
        ###############################################################################

        for model in features_dic.keys():
            logtime0 = datetime.datetime.now()

            vweights = 10**(config[model]['VERT_AGG']['BETA'] * (radartab['HEIGHT']/1000.)) # vert. weights

            filterconf = config[model]['FILTERING'].copy()
            logger.info('Computing data filter')
            logger.info('List of stations to ignore {:s}'.format(','.join(filterconf['STA_TO_REMOVE'])))
            logger.info('Start time {:s}'.format(str(tstart)))
            logger.info('End time {:s}'.format(str(tend)))           
            logger.info('ZH must be > {:f} if R <= {:f}'.format(filterconf['CONSTRAINT_MIN_ZH'][1],
                                                filterconf['CONSTRAINT_MIN_ZH'][0]))   
            logger.info('ZH must be < {:f} if R <= {:f}'.format(filterconf['CONSTRAINT_MAX_ZH'][1],
                                                filterconf['CONSTRAINT_MAX_ZH'][0]))    

            ZH_agg = vert_aggregation(pd.DataFrame(radartab['ZH_mean']),
                                        vweights,
                                        grp_vertical,
                                        True, radartab['VISIB_mean'])
            cond1 = np.array(np.isin(gaugetab['STATION'], filterconf['STA_TO_REMOVE']))
            cond2 = np.logical_and(ZH_agg['ZH_mean'] < filterconf['CONSTRAINT_MIN_ZH'][1],
                6 * gaugetab['RRE150Z0'].values >= filterconf['CONSTRAINT_MIN_ZH'][0])
            cond3 = np.logical_and(ZH_agg['ZH_mean'] >  filterconf['CONSTRAINT_MAX_ZH'][1],
                6 * gaugetab['RRE150Z0'].values <=  filterconf['CONSTRAINT_MIN_ZH'][0])
            
            invalid = np.logical_or(cond1,cond2)
            invalid = np.logical_or(invalid,cond3)
            invalid = np.logical_or(invalid,cond3)
            invalid = np.array(invalid)

            invalid[np.isnan(gaugetab['RRE150Z0'])] = 1

            ###############################################################################
            # Prepare training dataset
            ###############################################################################
        
            gaugetab_train = gaugetab[~invalid].copy()
        
            logger.info('Performing vertical aggregation of input features for model {:s}'.format(model))                
            features_VERT_AGG = vert_aggregation(radartab[features_dic[model]], 
                                 vweights, grp_vertical,
                                 config[model]['VERT_AGG']['VISIB_WEIGHTING'],
                                 radartab['VISIB_mean'])
            features_VERT_AGG = features_VERT_AGG[~invalid]
                        
            ###############################################################################
            # Fit
            ###############################################################################
            # create name of variables used in the model
            features = []
            for f in features_VERT_AGG.columns:
                if '_max' in f:
                    f = f.replace('_max','')
                elif '_min' in f:
                    f = f.replace('_min','')
                elif '_mean' in f:
                    f = f.replace('_mean','')
                features.append(f)
            
            Y = np.array(gaugetab_train['RRE150Z0'] * 6)
            valid = np.all(np.isfinite(features_VERT_AGG),axis=1)

            # Add some metadata
            config[model]['FILTERING']['N_datapoints'] = len(Y[valid])
            config[model]['FILTERING']['GAUGE_min_10min_mm_h'] = np.nanmin(Y[valid])
            config[model]['FILTERING']['GAUGE_max_10min_mm_h'] = np.nanmax(Y[valid])
            config[model]['FILTERING']['GAUGE_median_10min_mm_h'] = np.nanmedian(Y[valid])

            config[model]['FILTERING']['TIME_START'] = np.nanmin(gaugetab['TIMESTAMP'][~invalid])
            config[model]['FILTERING']['TIME_END'] = np.nanmax(gaugetab['TIMESTAMP'][~invalid])

            config[model]['FILTERING']['STA_INCLUDED'] = gaugetab['STATION'][~invalid].unique()
            config[model]['FILTERING']['CREATED'] = datetime.datetime.utcnow().strftime('%d %b %Y %H:%M UTC')

            logger.info('')
            logger.info('Training model on gauge data')

            logger.info('Initializing random forest model {:s}'.format(model))                
            if len(config[model]['QUANTILES']) == 0:
                reg = RandomForestRegressorBC(degree = 1, 
                          bctype = config[model]['BIAS_CORR'],
                          variables = features,
                          beta = config[model]['VERT_AGG']['BETA'],
                          visib_weighting=config[model]['VERT_AGG']['VISIB_WEIGHTING'],
                          **config[model]['RANDOMFOREST_REGRESSOR'])
            else:
                reg = QuantileRandomForestRegressorBC(degree = 1, 
                          bctype = config[model]['BIAS_CORR'],
                          variables = features,
                          beta = config[model]['VERT_AGG']['BETA'],
                          visib_weighting=config[model]['VERT_AGG']['VISIB_WEIGHTING'],
                          **config[model]['RANDOMFOREST_REGRESSOR'])
                # reg = QuantileRegressionForest(**config[model]['RANDOMFOREST_REGRESSOR'])
                # reg.bctype = config[model]['BIAS_CORR']
                # reg.variables = features
                # reg.beta = config[model]['VERT_AGG']['BETA']
                # reg.visib_weighting=config[model]['VERT_AGG']['VISIB_WEIGHTING']

            logger.info('Fitting random forest model {:s}'.format(model))

            reg.fit(features_VERT_AGG[valid].to_numpy(), Y[valid])

            logger.info('Model {} took {} minutes to be trained'.format(model, datetime.datetime.now()-logtime0))

            out_name = str(Path(output_folder, '{:s}_BETA_{:2.1f}_BC_{:s}.p'.format(model, 
                                                  config[model]['VERT_AGG']['BETA'],
                                                  config[model]['BIAS_CORR'])))
            logger.info('Saving model to {:s}'.format(out_name))
            
            pickle.dump(reg, open(out_name, 'wb'))

            del reg


class RFModelEval(object):
    '''
    This is the main class that allows to preparate data for random forest
    training, train random forests and perform cross-validation of trained models
    '''
    def __init__(self, db_location, input_location=None):
        """
        Initializes the class to analyse the model and perform standard ML 
        evaluation on it
        
        Note that when calling this constructor the input data is only 
        generated for the central pixel (NX = NY = 0 = loc of gauge), if you
        want to regenerate the inputs for all neighbour pixels, please 
        call the function self.prepare_input(only_center_pixel = False)
        
        Parameters
        ----------
        db_location : str
            Location of the main directory of the database (with subfolders
            'reference', 'gauge' and 'radar' on the filesystem)
        input_location : str
            Location of the prepared input data, if this data cannot be found
            in this folder, it will be computed here, default is a subfolder
            called rf_input_data within db_location
        force_regenerate_input : bool
            if True the input parquet files will always be regenerated from
            the database even if already present in the input_location folder
        """
        
        if input_location == None:
            input_location = str(Path(db_location, 'rf_input_data'))
            
        # Check if at least gauge.parquet, refer_x0y0.parquet and radar_x0y0.parquet
        # are present
        valid = True
        if not os.path.exists(input_location):
            valid = False
            os.makedirs(input_location)
        files = glob.glob(str(Path(input_location, '*')))
        files = [os.path.basename(f) for f in files]
        if ('gauge.parquet' not in files or 'reference_x0y0.parquet' not in files
            or 'radar_x0y0.parquet' not in files):
            valid = False
        
        self.input_location = input_location
        self.db_location = db_location
        
        if not valid :
            logger.info('Could not find valid input data from the folder {:s}'.format(input_location))

    def calc_model_predictions(self, models_dic, output_folder, model_folder=None,
                    tstart=None, tend=None, reference=['CPCH', 'RZC'],
                    station_obs = ['TRE200S0', 'DKL010Z0', 'FKL010Z0'],
                    ensemble = [] , quantile_dic = {}):
        """_summary_

        Parameters
        -----------
        models_dic : dic
            dic with modelname, filename of model
            e.g.,model_dic = {'RFO': 'RFO_BETA_-0.5_BC_spline.p'}
        output_folder : str
            Path where the dataframe is stored
        model_folder : str, optional
            Path where model is stored. Defaults to None.
        output_folder : str
            Path to where to store the scores
        tstart: str (YYYYMMDDHHMM)
            A date to define a starting time for the input data
        tend: str (YYYYMMDDHHMM)
            A date to define the end of the input data
        reference : list, optional 
            _description_. Defaults to ['CPCH', 'RZC'].
        station_obs : list, optional
            _description_. Defaults to ['TRE200S0', 'DKL1010Z0', 'FKL010Z0'].
        ensemble : list
            List with all models that a ensemble output is wished for
        quantile_dic : dic
            Dictionary with modelname and quantiles to extract,
            e.g., quantile_dic = {'QuRFO': [0.05,0.10,0.5,0.9,0.95]}
        """

        # Get models and model path
        modelnames = list(models_dic.keys())
        if model_folder == None:
            MODEL_FOLDER = self.model_paths
        else:
            MODEL_FOLDER = model_folder

        # Get data from database, and filter it according to the time limitations
        if len(reference) == 0 :
            gaugetab, radartab, grp_hourly, grp_vertical = \
                readInputData(self.input_location, tstart, tend, datalist=['gauge', 'radar'])
        else:
            gaugetab, radartab, refertab, grp_hourly, grp_vertical =\
                readInputData(self.input_location, tstart, tend, datalist=['gauge', 'radar', 'refer'])

        #################################################################################
        # Read models and create features dictionary
        #################################################################################
        regressors = {}
        features_dic = {}

        for model in modelnames:
            logger.info('Performing vertical aggregation of input features for model {:s}'.format(model))            
        
            # regressors[model] = pickle.load(open(Path(MODEL_FOLDER,features_dic[model]),'rb'))
            regressors[model] = read_rf(models_dic[model], MODEL_FOLDER)
            features = regressors[model].variables.copy()
            features_dic[model] = features
            
            # As RADAR_prop will be calculated below, adding variable here:
            # Compute additional data if needed
            features_to_be_removed = []
            for f in features:
                # Radar_prop is calculated with vert_aggregation
                if f.startswith('RADAR_prop'):
                    features_to_be_removed.append(f)

            if len(features_to_be_removed) > 0:
                for ftbr in features_to_be_removed:
                    features.remove(ftbr)
                features.append('RADAR')

            regressors[model].features = features.copy()

        ###############################################################################
        # Get linear units of reflectivity
        ###############################################################################
        radartab, features = processFeatures(features_dic, radartab)

        for colname in radartab.columns:
            list_feat = np.unique([item for sub in list(features_dic.values())
                        for item in sub])
            if colname.replace('_mean', '') in list_feat:
                radartab.rename(columns = {colname:colname.replace('_mean','')}, inplace=True)

        ###############################################################################
        # Compute vertical aggregation
        ###############################################################################
        features_VERT_AGG = {}
        for im, model in enumerate(modelnames):
            logger.info('Performing vertical aggregation of input features for model {:s}'.format(model))            

            beta = regressors[model].beta
            visib_weighting = regressors[model].visib_weighting

            if (im > 0) and (beta == regressors[modelnames[im-1]].beta) \
                    and (visib_weighting == regressors[modelnames[im-1]].visib_weighting) :
                logger.info('Model {} has same vertical aggregation settings as {}, hence just copy aggregated 2D fields'.format(model, modelnames[im-1]))
                features_VERT_AGG[model] = features_VERT_AGG[modelnames[im-1]].copy()
            else:
                vweights = 10**(beta*(radartab['HEIGHT']/1000.)) # vert. weights
                try:
                    features_VERT_AGG[model] = vert_aggregation(radartab[features_dic[model]],
                                        vweights, grp_vertical,
                                        visib_weighting,
                                        radartab['VISIB_mean'])
                except:
                    features_VERT_AGG[model] = vert_aggregation(radartab[features_dic[model]],
                                        vweights, grp_vertical,
                                        visib_weighting,
                                        radartab['VISIB'])                    

        ###############################################################################
        # Clean and prepare data
        ###############################################################################
        test_not_ok = False
        for iv, val in enumerate(radartab['s-tstamp'].groupby(grp_vertical).first()):
            if gaugetab['s-tstamp'].iloc[iv] != val:
                test_not_ok = True
                print(gaugetab['s-tstamp'][iv])
        if test_not_ok:
            logger.error('Time cut went wrong!!')
        
        valid = np.all(np.isfinite(features_VERT_AGG[modelnames[0]]),
                    axis = 1)

        for model in modelnames:
            features_VERT_AGG[model] = features_VERT_AGG[model][valid]

        gaugetab = gaugetab[valid]
        refertab = refertab[valid]
        grp_hourly = grp_hourly[valid]

        ###############################################################################
        # Assemble dataframe with data from database
        ###############################################################################
        logger.info('Assembling dataframe')
        data_pred = pd.DataFrame({'TIMESTAMP': gaugetab['TIMESTAMP'],
                            'STATION': gaugetab['STATION'],
                            'RRE150Z0': gaugetab['RRE150Z0']*6,
                            'IDX_HOURLY': grp_hourly})

        for var in station_obs:
            try:
                data_pred[var] = gaugetab[var]
            except:
                logger.error('Could not add {}'.format(var))                

        for ref in reference:
            try:
                data_pred[ref] = refertab[ref]
            except:
                logger.error('Could not add {}'.format(ref))

        ###############################################################################
        # Calculate model values
        ###############################################################################
        for model in modelnames:
            logger.info('Calculate estimates of {}'.format(model))

            if model in quantile_dic.keys():
                R_pred_10 = regressors[model].predict(features_VERT_AGG[model][regressors[model].variables],
                            quantiles=quantile_dic[model])
                data_pred[model] = R_pred_10[0]
                data_quant = pd.DataFrame(R_pred_10[1], 
                                        columns = [model+'_Q'+str(qu) for qu in quantile_dic[model]],
                                        index = data_pred.index)
                data_pred = pd.concat([data_pred, data_quant], axis=1)
            else:
                data_pred[model] = regressors[model].predict(\
                    features_VERT_AGG[model][regressors[model].variables])

            if (model in ensemble) & ('Qu' not in model) :
                pred_ens = regressors[model].predict_ens(\
                        features_VERT_AGG[model][regressors[model].variables])
                for tree in range(regressors[model].n_estimators):
                    data_pred['{}_E{}'.format(model, tree)] =  pred_ens[:,tree]
                    # data_pred['{}_E{}'.format(model, tree)] = \
                    #     regressors[model].estimators_[tree].predict(\
                    #     features_VERT_AGG[model][regressors[model].variables])

        ###############################################################################
        # Save output
        ###############################################################################
        name_file = str(Path(output_folder, 'rfmodels_x0_y0.parquet'))
        data_pred.to_parquet(name_file, compression='gzip', index=False)
        logger.info('Saved file: {}'.format(name_file))


    def feature_selection(self, features_dic, featuresel_configfile, 
                        output_folder, K=5, tstart=None, tend=None):
        """
        The relative importance of all available input vairables aggregated to 
        to the ground and to choose the most important ones, an approach 
        from Han et al. (2016) was adpated to for regression.
        See Wolfensberger et al. (2021) for further information.

        Parameters
        -----------
        features : dic
            A dictionnary with all eligible features to test
        feature_sel_config : str
            yaml file with setup
        output_folder : str
            Path to where to store the scores
        tstart: str (YYYYMMDDHHMM)
            A date to define a starting time for the input data
        tend: str (YYYYMMDDHHMM)
            A date to define the end of the input data
        K : int or None
            Number of splits in iterations do perform in the K fold cross-val
        """

        config = envyaml(featuresel_configfile)
        modelnames = list(features_dic.keys())


        ###############################################################################
        # Read data, filter time and get linear reflectivity units
        ###############################################################################
        gaugetab, radartab, _, grp_hourly, grp_vertical = \
            readInputData(self.input_location, tstart, tend)

        radartab, features = processFeatures(features_dic, radartab)

        ###############################################################################
        # Compute vertical aggregation
        ###############################################################################
        features_VERT_AGG = {}
        regressors = {}
        for im, model in enumerate(modelnames):
            logger.info('Performing vertical aggregation of input features for model {:s}'.format(model))            
          
            if (im > 0) and (config[model]['VERT_AGG']['BETA'] == config[modelnames[im-1]]['VERT_AGG']['BETA']) \
                    and (config[model]['VERT_AGG']['VISIB_WEIGHTING'] == config[modelnames[im-1]]['VERT_AGG']['VISIB_WEIGHTING']):
                logger.info('Model {} has same vertical aggregation settings as {}, hence just copy aggregated 2D fields'.format(model, modelnames[im-1]))
                features_VERT_AGG[model] = features_VERT_AGG[modelnames[im-1]].copy()
            else:
                vweights = 10**(config[model]['VERT_AGG']['BETA'] *
                                    (radartab['HEIGHT']/1000.)) # vert. weights
                features_VERT_AGG[model] = vert_aggregation(radartab[features_dic[model]], 
                                    vweights, grp_vertical,
                                    config[model]['VERT_AGG']['VISIB_WEIGHTING'],
                                    radartab['VISIB_mean'])

            regressors[model] = RandomForestRegressorBC(degree = 1, 
                        bctype = config[model]['BIAS_CORR'],
                        beta = config[model]['VERT_AGG']['BETA'],
                        variables = features_dic[model],
                        visib_weighting=config[model]['VERT_AGG']['VISIB_WEIGHTING'],
                        **config[model]['RANDOMFOREST_REGRESSOR'])
        
        # remove nans
        valid = np.all(np.isfinite(features_VERT_AGG[modelnames[0]]),
                       axis = 1)

        test_not_ok = False
        for iv, val in enumerate(radartab['s-tstamp'].groupby(grp_vertical).first()):
            if gaugetab['s-tstamp'][iv] != val:
                test_not_ok = True
                print(gaugetab['s-tstamp'][iv])
        if test_not_ok:
            logger.error('Time cut went wrong!!')

        for model in modelnames:
            features_VERT_AGG[model] = features_VERT_AGG[model][valid]
        
        gaugetab = gaugetab[valid]
        grp_hourly = grp_hourly[valid]
        
        # Get R, T and idx test/train
        R = np.array(gaugetab['RRE150Z0'] * 6) # Reference precip in mm/h
        R[np.isnan(R)] = 0

        ###############################################################################
        # Randomly split test/ train dataset
        ###############################################################################
        if (K != None):
            K = list(range(K))
        elif (K == None):
            logger.info('Cross validation with random events defined but not specified, applying 5-fold CV')
            K = list(range(5))

        idx_testtrain = split_event(gaugetab['TIMESTAMP'].values, len(K))

        ###############################################################################
        # Prepare score dictionnary
        ###############################################################################
        scores = {}
        for tagg in ['10min', '60min']:
            scores[tagg] = {}
            for model in modelnames:
                scores[tagg][model] = {}
                for feat in features_VERT_AGG[model].keys():
                    scores[tagg][model][feat] = []

        for k in K:
            logger.info('Run {:d}/{:d}-{:d} of cross-validation'.format(k,np.nanmin(K),np.nanmax(K)))

            test = idx_testtrain == k
            train = idx_testtrain != k

            for model in modelnames:
                # Model fit always based on 10min values
                regressors[model].fit(features_VERT_AGG[model][train],R[train])
                R_pred_10 = regressors[model].predict(features_VERT_AGG[model][test])

                # Get reference RMSE
                rmse_ref = perfscores(R_pred_10, R[test], bounds=None)['all']['RMSE']

                # At hourly values
                R_test_60 = np.squeeze(np.array(pd.DataFrame(R[test])
                            .groupby(grp_hourly[test]).mean()))
                R_pred_60 = np.squeeze(np.array(pd.DataFrame(R_pred_10)
                            .groupby(grp_hourly[test]).mean()))
                rmse_ref_60 = perfscores(R_pred_60, R_test_60, bounds=None)['all']['RMSE']

                for feat in features_VERT_AGG[model].keys():
                    logger.info('Shuffling feature: {}'.format(feat))
                    # Shuffle input feature on test fraction, keep others untouched
                    x_test = features_VERT_AGG[model][test].copy()
                    x_test[feat] = np.random.permutation(x_test[feat].values)

                    # Calculate estimates and shuffled RMSE
                    R_pred_shuffled = regressors[model].predict(x_test)

                    #Compute increase in RMSE score at 10min
                    rmse_shuff = perfscores(R_pred_shuffled, R[test], bounds=None)['all']['RMSE']
                    scores['10min'][model][feat].append((rmse_shuff - rmse_ref) / rmse_ref)
                    
                    #Compute increase in RMSE score at 60min
                    R_pred_shuffled_60 = np.squeeze(np.array(pd.DataFrame(R_pred_shuffled)
                                .groupby(grp_hourly[test]).mean()))
                    rmse_shuff_60 = perfscores(R_pred_shuffled_60, R_test_60, bounds=None)['all']['RMSE']
                    scores['60min'][model][feat].append((rmse_shuff_60- rmse_ref_60) / rmse_ref_60)

        # Save all output
        name_file = str(Path(output_folder, 'feature_selection_scores.p'))
        pickle.dump(scores, open(name_file, 'wb'))
             
    def model_intercomparison(self, features_dic, intercomparison_configfile, 
                              output_folder, reference_products = ['CPCH','RZC'],
                              bounds10 = [0,2,10,100], bounds60 = [0,2,10,100],
                              cross_val_type='years', K=5, years=None,
                              tstart=None, tend=None, station_scores=False,
                              save_model=False, save_output=False):
        """
        Does an intercomparison (cross-validation) of different RF models and
        reference products (RZC, CPC, ...) and plots the performance plots
        
        Parameters
        ----------
        features_dic : dict
            A dictionary whose keys are the names of the models you want to
            compare (a string) and the values are lists of features you want to
            use. For example {'RF_dualpol':['RADAR', 'zh_VISIB_mean',
            'zv_VISIB_mean','KDP_mean','RHOHV_mean','T', 'HEIGHT','VISIB_mean'],
            'RF_hpol':['RADAR', 'zh_VISIB_mean','T', 'HEIGHT','VISIB_mean']}
            will compare a model of RF with polarimetric info to a model
            with only horizontal polarization
        output_folder : str
            Location where to store the output plots
        intercomparison_config : str
            Location of the intercomparison configuration file, which
            is a yaml file that gives for every model key of features_dic which
            parameters of the training you want to use (see the file 
            intercomparison_config_example.yml in this module for an example)
        reference_products : list of str
            Name of the reference products to which the RF will be compared
            they need to be in the reference table of the database
        bounds10 : list of float
            list of precipitation bounds for which to compute scores separately
            at 10 min time resolution
            [0,2,10,100] will give scores in range [0-2], [2-10] and [10-100]
        bounds60 : list of float
            list of precipitation bounds for which to compute scores separately
            at hourly time resolution
            [0,1,10,100] will give scores in range [0-1], [1-10] and [10-100]
        cross_val_type: str
            Define how the split of events is done. Options are "random", 
            "years" and "seasons" (TODO)
        K : int or None
            Number of splits in iterations do perform in the K fold cross-val
        years : list or None
            List with the years that should be used in cross validation
            Default is [2016,2017,2018,2019,2020,2021]
        tstart: str (YYYYMMDDHHMM)
            A date to define a starting time for the input data
        tend: str (YYYYMMDDHHMM)
            A date to define the end of the input data
        station_scores: True or False (Boolean)
            If True, performance scores for all stations will be calculated
            If False, only the scores across Switzerland are calculated
        save_model: True or False (Boolean)
            If True, all models of the cross-validation are saved into a pickle file
            This is useful for reproducibility
        """
        
        # dict of statistics to compute for every score over the K-fold crossval,
        stats =  {'mean': np.nanmean, 'std': np.nanstd, 'min': np.nanmin, 'max': np.nanmax}
        
        config = envyaml(intercomparison_configfile)
        
        modelnames = list(features_dic.keys())
        keysconfig = list(config.keys())
        
        if not all([m in keysconfig for m in modelnames]):
            raise ValueError('Keys in features_dic are not all present in intercomparison config file!')
  
        if (cross_val_type == 'years') and (years == None):
            logger.info('Cross validation years defined, but not specified, years from 2016-2021 used')
            K = list(range(2016,2023,1))
        elif (cross_val_type == 'years') and (years != None):
            K = years

        if (cross_val_type == 'random') and (K != None):
            K = list(range(K))
        elif (cross_val_type == 'random') and (K == None):
            logger.info('Cross validation with random events defined but not specified, applying 5-fold CV')
            K = list(range(5))


        ###############################################################################
        # Read and filter data with time constraints
        ###############################################################################
        gaugetab, radartab, refertab, grp_hourly, grp_vertical = \
            readInputData(self.input_location, tstart, tend, 
                        datalist=['gauge', 'radar', 'refer'])

        radartab, _ = processFeatures(features_dic, radartab)
        ###############################################################################
        # Compute vertical aggregation and initialize model
        ###############################################################################

        features_VERT_AGG = {}
        regressors = {}
        for im, model in enumerate(modelnames):
            logger.info('Performing vertical aggregation of input features for model {:s}'.format(model))            
          
            # Save computational time if the input data is the same within the model
            if (im > 0) and (config[model]['VERT_AGG']['BETA'] == config[modelnames[im-1]]['VERT_AGG']['BETA']) \
                    and (config[model]['VERT_AGG']['VISIB_WEIGHTING'] == config[modelnames[im-1]]['VERT_AGG']['VISIB_WEIGHTING']):
                logger.info('Model {} has same vertical aggregation settings as {}, hence just copy aggregated 2D fields'.format(model, modelnames[im-1]))
                features_VERT_AGG[model] = features_VERT_AGG[modelnames[im-1]].copy()
            else:
                vweights = 10**(config[model]['VERT_AGG']['BETA'] *
                                    (radartab['HEIGHT']/1000.)) # vert. weights
                features_VERT_AGG[model] = vert_aggregation(radartab[features_dic[model]], 
                                    vweights, grp_vertical,
                                    config[model]['VERT_AGG']['VISIB_WEIGHTING'],
                                    radartab['VISIB_mean'])
            
            # Use either the classical RainForest model or the new one
            if len(config[model]['QUANTILES']) == 0:
                regressors[model] = RandomForestRegressorBC(degree = 1, 
                          bctype = config[model]['BIAS_CORR'],
                          variables = features_dic[model],
                          beta = config[model]['VERT_AGG']['BETA'],
                          visib_weighting=config[model]['VERT_AGG']['VISIB_WEIGHTING'],
                          **config[model]['RANDOMFOREST_REGRESSOR'])
            else:
                regressors[model] = QuantileRegressionForest(**config[model]['RANDOMFOREST_REGRESSOR'])

        ###############################################################################
        # Remove nans within dataset
        ###############################################################################
        valid = np.all(np.isfinite(features_VERT_AGG[modelnames[0]]),
                       axis = 1)
        for model in modelnames:
            features_VERT_AGG[model] = features_VERT_AGG[model][valid]

        gaugetab = gaugetab[valid]
        refertab = refertab[valid]
        grp_hourly = grp_hourly[valid]
        
        ###############################################################################
        # Get R, T and idx test/train
        ###############################################################################
        R = np.array(gaugetab['RRE150Z0'] * 6) # Reference precip in mm/h
        R[np.isnan(R)] = 0
        T = np.array(gaugetab['TRE200S0'])  # Reference temp in degrees

        if cross_val_type == 'random':
            idx_testtrain = split_event(gaugetab['TIMESTAMP'].values, len(K))
        elif cross_val_type == 'years':
            idx_testtrain = split_years(gaugetab['TIMESTAMP'].values, years=K)
        else:
            logger.error('Please define your cross validation separation')

        ###############################################################################
        # Initialize outputs
        ###############################################################################
        modelnames.extend(reference_products)

        if station_scores == False:
            all_scores, all_stats = prepareScoreDic(modelnames, station_scores)
        else:
            all_scores, all_stats, all_station_scores, all_station_stats = \
                    prepareScoreDic(modelnames, station_scores)
            
        ###############################################################################
        # MAIN LOOP: CROSS VALIDATION
        ###############################################################################
        for ik, k in enumerate(K):
            logger.info('Run {:d}/{:d}-{:d} of cross-validation'.format(k,np.nanmin(K),np.nanmax(K)))

            test = idx_testtrain == k
            train = idx_testtrain != k
            
            if cross_val_type == 'years':
                logger.info('Time range for testing set: {} - {} with {:3.2f}% of datapoints'.format(
                                datetime.datetime.utcfromtimestamp(gaugetab['TIMESTAMP'][test].min()),
                                datetime.datetime.utcfromtimestamp(gaugetab['TIMESTAMP'][test].max()),
                                gaugetab['TIMESTAMP'][test].count()/ gaugetab['TIMESTAMP'].count()*100))
            
            idxTemp = getTempIDX(T, grp_hourly, test, train)

            R_obs_test_60 = np.squeeze(np.array(pd.DataFrame(R[test])
                            .groupby(grp_hourly[test]).mean()))
    
            R_obs_train_60 = np.squeeze(np.array(pd.DataFrame(R[train])
                            .groupby(grp_hourly[train]).mean()))

            idxTemp['10min']['test']['all'] = R[test].astype(bool)
            idxTemp['10min']['train']['all'] = R[train].astype(bool)
            idxTemp['60min']['train']['all'] = R_obs_train_60.astype(bool)
            idxTemp['60min']['test']['all'] =  R_obs_test_60.astype(bool)

            # Fit every regression model
            for model in modelnames:
                logger.info('Checking model {:s}'.format(model))

                # Performing fit
                if model not in reference_products:
                    logger.info('Training model on gauge data')

                    regressors[model].fit(features_VERT_AGG[model][train].to_numpy(),R[train])

                    if len(config[model]['QUANTILES']) == 0 :
                        R_pred_10 = regressors[model].predict(features_VERT_AGG[model][test])
                    else:
                        # In this case, R_pred_10 becomes a tuple
                        R_pred_10_all = regressors[model].predict(features_VERT_AGG[model][test].to_numpy(),
                                                quantiles=config[model]['QUANTILES'])

                        # Save output for analysis
                        R_pred_10 = R_pred_10_all[0]

                        RFquantiles = np.column_stack([R_pred_10_all[0], R_pred_10_all[1]])
                        header = ['mean'] + [str(qe) for qe in config[model]['QUANTILES']]
                        RFQ = pd.DataFrame(RFquantiles, columns=header)
                        RFQ['gauge'] = R[test]
                        filename = str(Path(output_folder, 'RFQuantiles_{}_K_fold_{}.parquet'.format(model, k)))
                        logger.info('Saving data to {:s}'.format(filename))
                        RFQ.to_parquet(filename)

                    if (save_model == True):
                        regressors[model].variables = features_VERT_AGG[model].columns
                        out_name = str(Path(output_folder, '{:s}_BETA_{:2.1f}_BC_{:s}_excl_{}.p'.format(model, 
                                                            config[model]['VERT_AGG']['BETA'],
                                                            config[model]['BIAS_CORR'],k)))
                        logger.info('Saving model to {:s}'.format(out_name))
                        pickle.dump(regressors[model], open(out_name, 'wb'))

                else:
                    R_pred_10 = refertab[model].values[test]
                
                logger.info('Evaluating test error')
                logger.info('at 10 min')

                all_scores = calcScore(all_scores, obs=R[test], pred=R_pred_10,
                                idxTemp = idxTemp, bounds= bounds10, 
                                tagg = '10min', model=model, data_type='test')
    
                # 60 min
                logger.info('at 60 min')
                R_pred_60 = np.squeeze(np.array(pd.DataFrame(R_pred_10)
                                    .groupby(grp_hourly[test]).mean()))

                all_scores = calcScore(all_scores, obs=R_obs_test_60, pred=R_pred_60,
                                idxTemp = idxTemp, bounds= bounds60, 
                                tagg = '60min', model=model, data_type='test')
                
                if station_scores == True:
                    logger.info('Calculating station performances for model {}'.format(model)) 
                    
                    stations_60 = np.array(gaugetab['STATION'][test]
                                    .groupby(grp_hourly[test]).first())

                    df = pd.DataFrame(columns=gaugetab['STATION'].unique(),      
                                        index = all_scores['60min'][model]['test']['all'][ik]['all'].keys())

                    # Fast fix
                    liq_10_test = idxTemp['10min']['test']['liquid']
                    sol_10_test = idxTemp['10min']['test']['solid']
                    liq_60_test = idxTemp['60min']['test']['liquid'] 
                    sol_60_test = idxTemp['60min']['test']['solid']


                    for timeagg in all_station_scores.keys():
                        all_station_scores[timeagg][model]['all'][k] = df.copy()
                        all_station_scores[timeagg][model]['liquid'][k] = df.copy()
                        all_station_scores[timeagg][model]['solid'][k] = df.copy()
                    
                    for sta in gaugetab['STATION'].unique():
                        sta_idx = (gaugetab['STATION'][test] == sta)
                        sta_idx_60 = (stations_60 == sta)

                        try:                            
                            scores_all_10 = perfscores(R_pred_10[sta_idx],
                                                    R[test][sta_idx])['all']
                            all_station_scores['10min'][model]['all'][k][sta] = scores_all_10
                            
                            scores_all_60 = perfscores(R_pred_60[sta_idx_60],R_obs_test_60[sta_idx_60])['all']
                            all_station_scores['60min'][model]['all'][k][sta] = scores_all_60
                            
                            del scores_all_10, scores_all_60
                        except:
                            logger.info('No performance score for {}'.format(sta))
                        try:                            
                            scores_liquid_10 = perfscores(R_pred_10[liq_10_test & sta_idx],
                                                    R[test][liq_10_test & sta_idx])['all']
                            all_station_scores['10min'][model]['liquid'][k][sta] = scores_liquid_10
                            
                            scores_liquid_60 = perfscores(R_pred_60[liq_60_test & sta_idx_60],
                                                    R_obs_test_60[liq_60_test & sta_idx_60])['all']
                            all_station_scores['60min'][model]['liquid'][k][sta] = scores_liquid_60  
                        except:
                            logger.info('No performance score for liquid precip for {}'.format(sta))
                        try:                            
                            scores_solid_10 = perfscores(R_pred_10[sol_10_test & sta_idx],
                                                    R[test][sol_10_test & sta_idx])['all']  
                            all_station_scores['10min'][model]['solid'][k][sta] = scores_solid_10 
                            
                            scores_solid_60 = perfscores(R_pred_60[sol_60_test & sta_idx_60],
                                                        R_obs_test_60[sol_60_test & sta_idx_60])['all']
                            all_station_scores['60min'][model]['solid'][k][sta] = scores_solid_60
                        except:
                            logger.info('No performance score for solid precip for {}'.format(sta))

                # Save output of training and testing data
                if save_output:
                    if model not in reference_products:
                        data = features_VERT_AGG[model][test].copy()
                        data[model] = R_pred_10.copy()
                    else: 
                        data = pd.DataFrame(pd.Series(R_pred_10, name=model))
                    
                    data['gauge'] = R[test].copy()
                    data['TIMESTAMP'] = gaugetab['TIMESTAMP'][test]
                    data['STATION'] = gaugetab['STATION'][test]

                    out_name = str(Path(output_folder, 'data_{}_test_10min_CVfold_{}.csv'.format(model,k)))
                    data.to_csv(out_name, index=False)
                    logger.info('Saving data to {:s}'.format(out_name))

                    data_60 = pd.DataFrame({'gauge': R_obs_test_60, model: R_pred_60})
                    data_60['STATION'] = np.array(gaugetab['STATION'][test]
                                                .groupby(grp_hourly[test]).first())
                    data_60['TIMESTAMP'] = np.array(gaugetab['TIMESTAMP'][test]
                                                .groupby(grp_hourly[test]).max())
                    data_60['n_counts'] = np.squeeze(np.array(pd.DataFrame(R[test])
                                                .groupby(grp_hourly[test]).count()))
                    out_name = str(Path(output_folder, 'data_{}_test_60min_CVfold_{}.csv'.format(model,k)))
                    data_60.to_csv(out_name, index=False)
                    logger.info('Saving data to {:s}'.format(out_name))

                # train
                logger.info('Evaluating train error')
                # 10 min
                logger.info('at 10 min')
                
                if model not in reference_products:
                    R_pred_10 = regressors[model].predict(features_VERT_AGG[model][train])
                else:
                    R_pred_10 = refertab[model].values[train]
                    

                all_scores = calcScore(all_scores, obs=R[train], pred=R_pred_10,
                                idxTemp = idxTemp, bounds= bounds60, 
                                tagg = '10min', model=model, data_type='train')   

                # 60 min
                logger.info('at 60 min')
                R_pred_60 = np.squeeze(np.array(pd.DataFrame(R_pred_10)
                                    .groupby(grp_hourly[train]).mean()))

                all_scores = calcScore(all_scores, obs=R_obs_train_60, pred=R_pred_60,
                                idxTemp = idxTemp, bounds= bounds60, 
                                tagg = '60min', model=model, data_type='train')


        # Compute statistics after the 5-fold cross validation
        for agg in all_scores.keys():
            for model in all_scores[agg].keys():
                for veriftype in all_scores[agg][model].keys():
                    for preciptype in all_scores[agg][model][veriftype].keys():
                        bounds = list(all_scores[agg][model][veriftype][preciptype][0].keys())
                        scores =  all_scores[agg][model][veriftype][preciptype][0][bounds[0]].keys()
                        for bound in bounds:
                            all_stats[agg][model][veriftype][preciptype][bound] = {}
                            for score in scores:
                                data = all_scores[agg][model][veriftype][preciptype]
                                for d in data:
                                    if type(d[bound]) != dict:
                                        d[bound] = {'ME':np.nan,
                                                    'CORR':np.nan,
                                                    'STDE':np.nan,
                                                    'MAE':np.nan,
                                                    'scatter':np.nan,
                                                    'bias':np.nan,
                                                    'ED':np.nan}
                                datasc = [d[bound][score] for d in data]
                                all_stats[agg][model][veriftype][preciptype][bound][score] = {}
                                
                                for stat in stats.keys():
                                    sdata = stats[stat](datasc)
                                    all_stats[agg][model][veriftype][preciptype][bound][score][stat] = sdata

        if station_scores == True:
            for agg in all_station_scores.keys():
                for model in all_station_scores[agg].keys():
                    for preciptype in all_station_scores[agg][model].keys():
                        df = pd.DataFrame(columns=gaugetab['STATION'].unique())
                        perfs = {'RMSE': df.copy(), 'scatter':df.copy(), 
                                 'logBias':df.copy(), 'ED':df.copy(), 'N':df.copy(),
                                 'mest':df.copy(), 'mref':df.copy()}
                        all_station_stats[agg][model][preciptype] = {}
                        for score in perfs.keys():
                            for kidx in all_station_scores[agg][model][preciptype].keys():
                                df_dummy = all_station_scores[agg][model][preciptype][kidx].copy()
                                perfs[score] = perfs[score].append(df_dummy.loc[df_dummy.index == score])
            
                            all_station_stats[agg][model][preciptype][score] = pd.concat([perfs[score].mean().rename('mean'),
                                                                                          perfs[score].std().rename('std')],
                                                                                         axis=1)

        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        name_file = str(Path(output_folder, 'all_scores.p'))
        pickle.dump(all_scores, open(name_file, 'wb'))
        name_file = str(Path(output_folder, 'all_scores_stats.p'))
        pickle.dump(all_stats, open(name_file, 'wb'))     
        
        if station_scores == True:
            name_file = str(Path(output_folder, 'all_station_scores.p'))
            pickle.dump(all_station_scores, open(name_file, 'wb'))
            name_file = str(Path(output_folder, 'all_station_stats.p'))
            pickle.dump(all_station_stats, open(name_file, 'wb'))

        plot_crossval_stats(all_stats, output_folder)
        
        logger.info('Finished script and saved all scores to {}'.format(output_folder))
        return all_scores, all_stats
            
