# -*- coding: utf-8 -*-

"""
***************************************************************************
    SagaAlgorithm.py
    ---------------------
    Date                 : August 2012
    Copyright            : (C) 2012 by Victor Olaya
    Email                : volayaf at gmail dot com
***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*                                                                         *
***************************************************************************
"""


__author__ = 'Victor Olaya'
__date__ = 'August 2012'
__copyright__ = '(C) 2012, Victor Olaya'

# This will get replaced with a git SHA1 when you do a git archive

__revision__ = '$Format:%H$'

import os
import importlib
from copy import deepcopy
from qgis.core import (QgsProcessingUtils,
                       QgsProcessingException,
                       QgsMessageLog,
                       QgsProcessing,
                       QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterBoolean,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterEnum,
                       QgsProcessingParameterMultipleLayers,
                       QgsProcessingParameterMatrix,
                       QgsProcessingParameterString,
                       QgsProcessingParameterField,
                       QgsProcessingParameterFile,
                       QgsProcessingParameterExtent,
                       QgsProcessingParameterRasterDestination,
                       QgsProcessingParameterVectorDestination)
from processing.core.ProcessingConfig import ProcessingConfig
from processing.core.parameters import getParameterFromString
from processing.algs.help import shortHelp
from processing.tools.system import getTempFilename
from processing.algs.saga.SagaNameDecorator import decoratedAlgorithmName, decoratedGroupName
from . import SagaUtils
from .SagaAlgorithmBase import SagaAlgorithmBase

pluginPath = os.path.normpath(os.path.join(
    os.path.split(os.path.dirname(__file__))[0], os.pardir))

sessionExportedLayers = {}


class SagaAlgorithm(SagaAlgorithmBase):

    OUTPUT_EXTENT = 'OUTPUT_EXTENT'

    def __init__(self, descriptionfile):
        super().__init__()
        self.hardcoded_strings = []
        self.allow_nonmatching_grid_extents = False
        self.description_file = descriptionfile
        self.undecorated_group = None
        self._name = ''
        self._display_name = ''
        self._group = ''
        self.params = []
        self.defineCharacteristicsFromFile()

    def createInstance(self):
        return SagaAlgorithm(self.description_file)

    def initAlgorithm(self, config=None):
        for p in self.params:
            self.addParameter(p)

    def name(self):
        return self._name

    def displayName(self):
        return self._display_name

    def group(self):
        return self._group

    def shortHelpString(self):
        return shortHelp.get(self.id(), None)

    def defineCharacteristicsFromFile(self):
        with open(self.description_file) as lines:
            line = lines.readline().strip('\n').strip()
            self._name = line
            if '|' in self._name:
                tokens = self._name.split('|')
                self._name = tokens[0]
                # cmdname is the name of the algorithm in SAGA, that is, the name to use to call it in the console
                self.cmdname = tokens[1]

            else:
                self.cmdname = self._name
                self._display_name = self.tr(str(self._name))
            self._name = decoratedAlgorithmName(self._name)
            self._display_name = self.tr(str(self._name))

            self._name = self._name.lower()
            validChars = \
                'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:'
            self._name = ''.join(c for c in self._name if c in validChars)

            line = lines.readline().strip('\n').strip()
            self.undecorated_group = line
            self._group = self.tr(decoratedGroupName(self.undecorated_group))
            line = lines.readline().strip('\n').strip()
            while line != '':
                if line.startswith('Hardcoded'):
                    self.hardcoded_strings.append(line[len('Hardcoded|'):])
                elif line.startswith('QgsProcessingParameter') or line.startswith('Parameter'):
                    self.params.append(getParameterFromString(line))
                elif line.startswith('AllowUnmatching'):
                    self.allow_nonmatching_grid_extents = True
                elif line.startswith('Extent'):
                    # An extent parameter that wraps 4 SAGA numerical parameters
                    self.extentParamNames = line[6:].strip().split(' ')
                    self.params.append(QgsProcessingParameterExtent(self.OUTPUT_EXTENT,
                                                                    'Output extent'))
                else:
                    pass # TODO
                    #self.addOutput(getOutputFromString(line))
                line = lines.readline().strip('\n').strip()

    def processAlgorithm(self, parameters, context, feedback):
        commands = list()
        self.exportedLayers = {}

        self.preProcessInputs()
        extent = None

        # 1: Export rasters to sgrd and vectors to shp
        # Tables must be in dbf format. We check that.
        for param in self.parameterDefinitions():
            if isinstance(param, QgsProcessingParameterRasterLayer):
                if param.name() not in parameters or parameters[param.name()] is None:
                    continue
                if parameters[param.name()].endswith('sdat'):
                    parameters[param.name()] = parameters[param.name()][:-4] + "sgrd"
                elif not parameters[param.name()].endswith('sgrd'):
                    exportCommand = self.exportRasterLayer(parameters[param.name()], context)
                    if exportCommand is not None:
                        commands.append(exportCommand)
            elif isinstance(param, QgsProcessingParameterFeatureSource):
                if param.name() not in parameters or parameters[param.name()] is None:
                    continue

                if not crs:
                    source = self.parameterAsSource(parameters, param.name(), context)
                    crs = source.sourceCrs()

                layer_path = self.parameterAsCompatibleSourceLayerPath(parameters, param.name(), context, ['shp'], 'shp', feedback=feedback)
                if layer_path:
                    self.exportedLayers[param.name()] = layer_path
                else:
                    raise QgsProcessingException(
                        self.tr('Unsupported file format'))
            elif isinstance(param, QgsProcessingParameterMultipleLayers):
                if param.name() not in parameters or parameters[param.name()] is None:
                    continue
                layers = self.parameterAsLayerList(parameters, param.name(), context)
                if layers is None or len(layers) == 0:
                    continue
                if param.layerType() == QgsProcessing.TypeRaster:
                    for i, layerfile in enumerate(layers):
                        if layerfile.endswith('sdat'):
                            layerfile = param.value[:-4] + "sgrd"
                            layers[i] = layerfile
                        elif not layerfile.endswith('sgrd'):
                            exportCommand = self.exportRasterLayer(layerfile)
                            if exportCommand is not None:
                                commands.append(exportCommand)
                        param.value = ";".join(layers)
                else:
                    temp_params = deepcopy(parameters)
                    for layer in layers:
                        temp_params[param.name()] = layer

                        if not crs:
                            source = self.parameterAsSource(temp_params, param.name(), context)
                            crs = source.sourceCrs()

                        layer_path = self.parameterAsCompatibleSourceLayerPath(temp_params, param.name(), context, 'shp',
                                                                               feedback=feedback)
                        if layer_path:
                            if param.name() in self.exportedLayers:
                                self.exportedLayers[param.name()].append(layer_path)
                            else:
                                self.exportedLayers[param.name()] = [layer_path]
                        else:
                            raise QgsProcessingException(
                                self.tr('Unsupported file format'))

        # 2: Set parameters and outputs
        command = self.undecorated_group + ' "' + self.cmdname + '"'
        command += ' ' + ' '.join(self.hardcoded_strings)

        for param in self.parameterDefinitions():
            if not param.name() in parameters or parameters[param.name()] is None:
                continue
            if param.isDestination():
                continue

            if isinstance(param, (QgsProcessingParameterRasterLayer, QgsProcessingParameterFeatureSource)):
                command += ' -' + param.name() + ' "' \
                    + self.exportedLayers[param.name()] + '"'
            elif isinstance(param, QgsProcessingParameterMultipleLayers):
                s = parameters[param.name()]
                for layer in list(self.exportedLayers.keys()):
                    s = s.replace(layer, self.exportedLayers[layer])
                command += ' -' + ';'.join(self.exportedLayers[param.name()]) + ' "' + s + '"'
            elif isinstance(param, QgsProcessingParameterBoolean):
                if self.parameterAsBool(parameters, param.name(), context):
                    command += ' -' + param.name().strip() + " true"
                else:
                    command += ' -' + param.name().strip() + " false"
            elif isinstance(param, QgsProcessingParameterMatrix):
                tempTableFile = getTempFilename('txt')
                with open(tempTableFile, 'w') as f:
                    f.write('\t'.join([col for col in param.headers()]) + '\n')
                    values = self.parameterAsMatrix(parameters, param.name(), context)
                    for i in range(0, len(values), 3):
                        s = values[i] + '\t' + values[i + 1] + '\t' + values[i + 2] + '\n'
                        f.write(s)
                command += ' -' + param.name() + ' "' + tempTableFile + '"'
            elif isinstance(param, QgsProcessingParameterExtent):
                # 'We have to substract/add half cell size, since SAGA is
                # center based, not corner based
                halfcell = self.getOutputCellsize(parameters, context) / 2
                offset = [halfcell, -halfcell, halfcell, -halfcell]
                rect = self.parameterAsExtent(parameters, param.name(), context)

                values = []
                values.append(rect.xMinimum())
                values.append(rect.yMinimum())
                values.append(rect.xMaximum())
                values.append(rect.yMaximum())

                for i in range(4):
                    command += ' -' + self.extentParamNames[i] + ' ' \
                        + str(float(values[i]) + offset[i])
            elif isinstance(param, QgsProcessingParameterNumber):
                command += ' -' + param.name() + ' ' + str(self.parameterAsDouble(parameters, param.name(), context))
            elif isinstance(param, QgsProcessingParameterEnum):
                command += ' -' + param.name() + ' ' + str(self.parameterAsEnum(parameters, param.name(), context))
            elif isinstance(param, (QgsProcessingParameterString, QgsProcessingParameterFile)):
                command += ' -' + param.name() + ' "' + self.parameterAsFile(parameters, param.name(), context) + '"'
            elif isinstance(param, (QgsProcessingParameterString, QgsProcessingParameterField)):
                command += ' -' + param.name() + ' "' + self.parameterAsString(parameters, param.name(), context) + '"'

        output_layers = []
        output_files = {}
        for out in self.destinationParameterDefinitions():
            # TODO
            # command += ' -' + out.name() + ' "' + out.getCompatibleFileName(self) + '"'
            file = self.parameterAsOutputLayer(parameters, out.name(), context)
            if isinstance(out, (QgsProcessingParameterRasterDestination, QgsProcessingParameterVectorDestination)):
                output_layers.append(file)
            output_files[out.name()] = file
            command += ' -' + out.name() + ' "' + file + '"'

        commands.append(command)

        # special treatment for RGB algorithm
        # TODO: improve this and put this code somewhere else
        for out in self.destinationParameterDefinitions():
            if isinstance(out, QgsProcessingParameterRasterDestination):
                filename = out.getCompatibleFileName(self)
                filename2 = filename + '.sgrd'
                if self.cmdname == 'RGB Composite':
                    commands.append('io_grid_image 0 -IS_RGB -GRID:"' + filename2 +
                                    '" -FILE:"' + filename + '"')

        # 3: Run SAGA
        commands = self.editCommands(commands)
        SagaUtils.createSagaBatchJobFileFromSagaCommands(commands)
        loglines = []
        loglines.append(self.tr('SAGA execution commands'))
        for line in commands:
            feedback.pushCommandInfo(line)
            loglines.append(line)
        if ProcessingConfig.getSetting(SagaUtils.SAGA_LOG_COMMANDS):
            QgsMessageLog.logMessage('\n'.join(loglines), self.tr('Processing'), QgsMessageLog.INFO)
        SagaUtils.executeSaga(feedback)

        if crs is not None:
            for out in output_layers:
                prjFile = os.path.splitext(out)[0] + ".prj"
                with open(prjFile, "w") as f:
                    f.write(crs.toWkt())

        result = {}
        for o in self.outputDefinitions():
            if o.name() in output_files:
                result[o.name()] = output_files[o.name()]
        return result

    def preProcessInputs(self):
        name = self.name().replace('.', '_')
        try:
            module = importlib.import_module('processing.algs.saga.ext.' + name)
        except ImportError:
            return
        if hasattr(module, 'preProcessInputs'):
            func = getattr(module, 'preProcessInputs')
            func(self)

    def editCommands(self, commands):
        try:
            module = importlib.import_module('processing.algs.saga.ext.' + self.name())
        except ImportError:
            return commands
        if hasattr(module, 'editCommands'):
            func = getattr(module, 'editCommands')
            return func(commands)
        else:
            return commands

    def getOutputCellsize(self, parameters, context):
        """Tries to guess the cell size of the output, searching for
        a parameter with an appropriate name for it.
        :param parameters:
        """

        cellsize = 0
        for param in self.parameterDefinitions():
            if param.name() in parameters and param.name() == 'USER_SIZE':
                cellsize = self.parameterAsDouble(parameters, param.name(), context)
                break
        return cellsize

    def exportRasterLayer(self, source, context):
        global sessionExportedLayers
        if source in sessionExportedLayers:
            exportedLayer = sessionExportedLayers[source]
            if os.path.exists(exportedLayer):
                self.exportedLayers[source] = exportedLayer
                return None
            else:
                del sessionExportedLayers[source]
        layer = QgsProcessingUtils.mapLayerFromString(source, context, False)
        if layer:
            filename = str(layer.name())
        else:
            filename = os.path.basename(source)
        validChars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:'
        filename = ''.join(c for c in filename if c in validChars)
        if len(filename) == 0:
            filename = 'layer'
        destFilename = QgsProcessingUtils.generateTempFilename(filename + '.sgrd')
        self.exportedLayers[source] = destFilename
        sessionExportedLayers[source] = destFilename
        return 'io_gdal 0 -TRANSFORM 1 -RESAMPLING 3 -GRIDS "' + destFilename + '" -FILES "' + source + '"'

    def checkParameterValues(self, parameters, context):
        """
        We check that there are no multiband layers, which are not
        supported by SAGA, and that raster layers have the same grid extent
        """
        extent = None
        for param in self.parameterDefinitions():
            files = []
            if isinstance(param, QgsProcessingParameterRasterLayer):
                files = [parameters[param.name()]]
            elif (isinstance(param, QgsProcessingParameterMultipleLayers) and
                    param.datatype == QgsProcessing.TypeRaster):
                if parameters[param.name()] is not None:
                    files = parameters[param.name()]
            for f in files:
                layer = QgsProcessingUtils.mapLayerFromString(f, context)
                if layer is None:
                    continue
                if layer.bandCount() > 1:
                    return False, self.tr('Input layer {0} has more than one band.\n'
                                          'Multiband layers are not supported by SAGA').format(layer.name())
                if not self.allow_nonmatching_grid_extents:
                    if extent is None:
                        extent = (layer.extent(), layer.height(), layer.width())
                    else:
                        extent2 = (layer.extent(), layer.height(), layer.width())
                        if extent != extent2:
                            return False, self.tr("Input layers do not have the same grid extent.")
        return super(SagaAlgorithm, self).checkParameterValues(parameters, context)
