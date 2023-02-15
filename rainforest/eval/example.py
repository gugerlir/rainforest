#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""

Example to compile a full performance assessment of a precipitation event 
The performance assessment is based on QPE maps

"""

import os
os.environ['RAINFOREST_DATAPATH'] = '/store/msrad/radar/rainforest/rainforest_data/'

from compileMapEstimates import compileMapEstimates
from calcPerfscores import *
from plotPerfscores import *

config_file = '/scratch/rgugerli/data4Rad4Alp/config_eval.yml'

# Compile dataset
eval = compileMapEstimates(config_file, overwrite=True)

# Calculate scores
calcPerf = calcPerfscores(configfile=config_file, read_only=False)
scores = calcPerf.scores

# Plotting routines
tagg = '10min'; ith=0.6
filename = calcPerf.mainfolder+'/results/'+'map_bias_{}_{}_DB_{}.png'.format(calcPerf.datestring, tagg, str(ith).replace('.','_'))
fig = plotModelMapsSubplots(scores[tagg][ith], calcPerf.modellist, score='BIAS', filename=filename)

filename = calcPerf.mainfolder+'/results/'+'map_scatter_{}_{}_DB_{}.png'.format(calcPerf.datestring, tagg, str(ith).replace('.','_'))
fig = plotModelMapsSubplots(scores[tagg][ith], calcPerf.modellist, score='SCATTER', filename=filename)