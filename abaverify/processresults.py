"""
Opens an abaqus odb and gets requested results.

Arguments
---------
    jobName: 
        name of the abaqus job (code expects an odb and a results text file with this name
        in the working dir)

Example
-------
$ abaqus cae noGUI=model_runner.py -- jobName

Returns
-------
This code writes a file jobName_results.py with the reference value and the results collected from the job

Notes
-----
Test types:
1. max, min:
    Records the maximum or minimum value of the specified identifier. Assumes that only one identifier is specified.
2. continuous:
    Records the maximum change in the specified identifier value between each increment. This test should fail
    when a discontinuity occurs that is larger than the reference value + tolerance. Assumes that only one
    identifier is specified.
3. xy_infl_pt:
    Records the location of an inflection point in an x-y data set. Assumes two identifiers are specified where
    the first is the x-data and the second is the y-data. To speed up the search, a window can be specified
    as [min, max] where only values in x withing the bounds of the window are used in searching for the inflection
    point. The reference value and tolerance are specified as pairs corresponding to the x and y values [x, y].
    Numerical derivatives are calculated to find the inflection point. A Butterworth filter is used to smooth the data.
    It is possible that this test could produce erroneous results if the data is too noisy for the filter settings.
4. disp_at_zero_y:
    Records the displacement when the y-value reaches zero. This is useful for finding the maximum displacement in cohesive
    laws.
5. log_stress_at_failure_init:
    Writes specified stresses (stressComponents) when first failure index in failureIndices indicates failure (>= 1.0).
    This test assumes that the input deck terminates the analysis shortly after failure is reached by using the *Filter card.
    All of the failure indices to check for failure should be included in the failureIndices array. The option
    additionalIdenitifiersToStore can be used to record additional outputs at the increment where failure occurs
    (e.g., alpha). Stress in stressComponents and other data in additionalIdenitifiersToStore are written to a log
    file that is named using the test base name.
6. slope
    Finds the slope of an x-y data curve.
7. finalValue
    Last output value.
8. tabular
    Compares the values for a list of tuples specifying x, y points [(x1, y1), (x2, y2)...]

"""


from abaqus import *
from abaqusConstants import *
from caeModules import *
import os
import inspect
import sys
import re
import numpy as np
from operator import itemgetter

# This is a crude hack, but its working
pathForThisFile = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
sys.path.insert(0, pathForThisFile)


# Throw-away debugger
def debug(obj):
    if True:   # Use to turn off debugging
        sys.__stderr__.write("DEBUG - " + __name__ + ":  " + str(obj) + '\n')


def interpolate(x, xp, fp):
    """
    Augmentation to np.interp to handle negative numbers. This is a hack, needs improvement.
    """
    if np.all(np.diff(xp) > 0):
        return np.interp(x, xp, fp)
    else:
        if all([i < 0 for i in xp]):
            xpos = np.absolute(xp)
            return np.interp(-1 * x, xpos, fp)
        else:
            raise Exception("Functionality to interpolate datasets that traverse 0 or are unsorted is not implemented")


def resample(data, numPts):
    """
    Re-samples an xy data set to have the number of points specified and a linear space in x
    """
    x, y = zip(*data)
    if x[0] < x[-1]:
        xMin = x[0]
        xMax = x[-1]
    else:
        xMin = x[-1]
        xMax = x[0]
    xNew = np.linspace(xMin, xMax, numPts)
    yNew = interpolate(xNew, x, y)
    return zip(xNew, yNew)


def listOfHistoryOutputSymbols():
    """
    Generates a list of the history output symbols in the odb
    """
    outputs = list()
    for s in odb.steps.keys():
        for hr in odb.steps[s].historyRegions.keys():
            for ho in odb.steps[s].historyRegions[hr].historyOutputs.keys():
                if ho not in outputs:
                    outputs.append(ho)
    return outputs


def parseJobName(name):
    """
    Parses job name of parametric tests
    """

    s = name.split("_")

    output = dict()

    # Find the index of the first integer (value of parameter)
    idxFirstInt = 0
    while True:
        try:
            int(s[idxFirstInt])
            break
        except ValueError:
            idxFirstInt += 1

    # Everything before the first integer is the basename
    output["baseName"] = "_".join(s[0:idxFirstInt - 1])

    # Assume everything after the basename is parameters and values
    for n in range(idxFirstInt - 1, len(s), 2):
        output[s[n]] = s[n + 1]

    return output


def _historyOutputNameHelperNode(prefixString, identifier, steps):
    """ Helper to prevent repeated code in historyOutputNameFromIdentifier """

    i = str(identifier["symbol"])
    if "position" in identifier and "nset" in identifier:
        return prefixString + i + " at " + str(identifier["position"]) + " in NSET " + str(identifier["nset"])
    elif "nset" in identifier:
        if len(steps) > 1:
            raise Exception("_historyOutputNameHelperNode: Must specify position if analysis has multiple steps")
        else:
            step = steps[0]
        for historyRegion in odb.steps[step].historyRegions.keys():
            if i in odb.steps[step].historyRegions[historyRegion].historyOutputs.keys():
                regionLabels = historyRegion
                labelSplit = historyRegion.split(' ')
                debug(labelSplit)
                if labelSplit[0] == 'Node' and len(labelSplit) == 2:
                    nodeNumber = labelSplit[1].split('.')[1]
                    return prefixString + i + " at Node " + nodeNumber + " in NSET " + str(identifier["nset"])
                else:
                    raise Exception("Must specify a position for " + i)
    else:
        raise ValueError("Missing nset definition in RF identifier")

    raise Exception("Error getting history Output name in _historyOutputNameHelperNode")


def _historyOutputNameHelperElement(prefixString, identifier, steps):
    """ Helper to prevent repeated code in historyOutputNameFromIdentifier """

    i = str(identifier["symbol"])
    if "position" in identifier and "elset" in identifier:
        if i not in listOfHistoryOutputSymbols():
            for sym in listOfHistoryOutputSymbols():
                if i.lower() == sym.lower():
                    i = sym
        return prefixString + i + " at " + str(identifier["position"]) + " in ELSET " + str(identifier["elset"])
    else:
        raise ValueError("Must define position in element identifier")


def historyOutputNameFromIdentifier(identifier, steps=None):
    """
    Returns a well-formated history output name from the identifier. The identifier can be in any of a variety of
    formats. A list of identifiers can be provided in which case this function returns a tuple of history output names.

    The step is the name of the step of interest.


    Accepted identifier formats:
    # Specify the complete identifier
    - "identifier": "Reaction force: RF1 at Node 9 in NSET LOADAPP"
    # Specify the complete identifier piecemeal
    - "identifier": {
                "symbol": "RF1",
                "position": "Node 9",
                "nset": "LOADAPP"
            }
    # For element output data, "elset" can be used
    - "identifier": {
                "symbol": "S11",
                "position": "Element 1 Int Point 1",
                "elset": "DAMAGEABLEROW"
            }
    # Specify the identifier without a position. In this case, the code looks through the available history outputs for a
    # valid match. If a single match is found, it is used. Otherwise an exception is raised. The step argument must be specified.
    - "identifier": {
                "symbol": "RF1",
                "nset": "LOADAPP"
            }
    # Not implemented for an element (yet)
    """

    # Handle case of a list of identifiers
    if isinstance(identifier, list):
        out = list()
        for i in identifier:
            out.append(historyOutputNameFromIdentifier(identifier=i, steps=steps))
        return tuple(out)

    # Case when identifier is a dictionary
    elif isinstance(identifier, dict):
        if "symbol" in identifier:
            i = str(identifier["symbol"])

            # Parse the symbol. Known symbols (RF, U, S, LE, E, SDV)
            # RF
            if re.match(r'^RF\d', i):
                return _historyOutputNameHelperNode(prefixString="Reaction force: ", identifier=identifier, steps=steps)

            # U
            elif re.match(r'^U\d', i):
                return _historyOutputNameHelperNode(prefixString="Spatial displacement: ", identifier=identifier, steps=steps)

            # S
            elif re.match(r'^S\d+', i):
                return _historyOutputNameHelperElement(prefixString="Stress components: ", identifier=identifier, steps=steps)

            # LE
            elif re.match(r'^LE\d+', i):
                return _historyOutputNameHelperElement(prefixString="Logarithmic strain components: ", identifier=identifier, steps=steps)

            # E
            elif re.match(r'^E\d+', i):
                raise NotImplementedError()

            # SDV
            elif re.match(r'^SDV\w+', i):
                return _historyOutputNameHelperElement(prefixString="Solution dependent state variables: ", identifier=identifier, steps=steps)

            else:
                raise ValueError("Unrecognized symbol " + i + " found")
        else:
            raise ValueError("Identifier missing symbol definition")

    # Case when the identifer is specified directly
    elif isinstance(identifier, (str, unicode)):
        return str(identifier)
    else:
        raise ValueError("Expecting that the argument is a list, dict, or str. Found " + str(type(identifier)))


def write_results(results_to_write, fileName, depth=0):
    """
    Writes the contents of resultsFile to a Python text file

    :type results_to_write: list or dictionary
    :type fileName: str
    :type depth: int
    """

    debug(fileName)
    # Create the file if it does not exist. If it does exist, open it for appending
    if depth == 0:
        f = open(fileName, 'w')
        f.write('results = ')
    else:
        f = open(fileName, 'a')

    if isinstance(results_to_write, dict):

        f.write('\t' * depth + '{\n')

        for r in results_to_write:

            f.write('\t' * depth + '"' + r + '": ')

            if isinstance(results_to_write[r], (dict, list)):
                f.write('\n')
                f.close()
                write_results(results_to_write[r], fileName, depth + 1)
                f = open(fileName, 'a')

            elif isinstance(results_to_write[r], str):
                f.write('"' + str(results_to_write[r]) + '",\n')
            else:
                f.write(str(results_to_write[r]) + ',\n')

        if depth == 0:
            f.write('\t' * depth + '}\n')
        else:
            f.write('\t' * depth + '}, \n')

    elif isinstance(results_to_write, list):

        f.write('\t' * depth + '[\n')

        for r in results_to_write:

            if isinstance(r, (dict, list)):
                f.close()
                write_results(r, fileName, depth + 1)
                f = open(fileName, 'a')

            elif isinstance(r, str):
                f.write('\t' * depth + '"' + str(r) + '", \n')

            else:
                f.write('\t' * depth + str(r) + ', \n')

        if depth == 0:
            f.write('\t' * depth + ']\n')
        else:
            f.write('\t' * depth + '],\n')


def evaluate_statement(identifier_list, var_names, eval_statement):
    '''

    :param identifier_list: list of dictionaries which have information about what XYDataFromHistory to pull
    :param var_names: a list (with a matching correspondence in identifier_list) which is the well formed variable name
                      used for interroggating xyDataFromHistory
    :param eval_statement: a str statement evaluated using python eval. By convention variables should be accesses
                           by using "d[key]". d is a local variable created which maps the label to its
                           corresponding xydata.
    :return: an xydata object returned by evaluating eval
    '''

    label_missing_err_message = "The identifiers specified in identifier_list must" \
                                " all contain the label key. The following does not: {}".format(identifier_list)
    assert all([label_name in ident for ident in identifier_list]), label_missing_err_message
    # Build local variable d (maps label -> corresponding data
    d = {}
    for (ident, var_name) in zip(identifier_list, var_names):
        data = session.XYDataFromHistory(name=str(ident[symbol_name]), odb=odb,
                                      outputVariableName=var_name,
                                      steps=steps)
        d[ident[label_name]] = data
    # A well formed eval_statement accesses the local variable d
    return eval(eval_statement)


def getX(r, varNames):
    # If specify eval statements than x, y aren't positional
    # but instead are calculated using the uniquely defined label val to access variables
    if xEvalStatement in r and yEvalStatement in r:
        x = evaluate_statement(identifier_list=r[identifier_name], var_names=varNames,
                               eval_statement=r[xEvalStatement])
    else:
        # Get xy data (positionally)
        x = session.XYDataFromHistory(name=str(r[identifier_name][0][symbol_name]), odb=odb,
                                      outputVariableName=varNames[0],
                                      steps=steps)
    return x

def getY(r, varNames):
    # If specify eval statements than x, y aren't positional
    # but instead are calculated using the uniquely defined label val to access variables
    if xEvalStatement in r and yEvalStatement in r:
        y = evaluate_statement(identifier_list=r[identifier_name], var_names=varNames,
                               eval_statement=r[yEvalStatement])
    else:
        # Get xy data (positionally)
        y = session.XYDataFromHistory(name=str(r[identifier_name][1][symbol_name]), odb=odb,
                                      outputVariableName=varNames[1],
                                      steps=steps)
    return y

def getXY(r, varNames):
    x = getX(r, varNames)
    y = getY(r, varNames)
    return x, y

# keys used in results dict
type_name = "type"
compute_value_name = "computedValue"
reference_value_name = "referenceValue"
identifier_name = "identifier"
symbol_name = "symbol"
results_name = "results"
results_max = "max"
results_min = "min"
results_continous = "continuous"
results_xy_infl_pt = "xy_infl_pt"
results_disp_at_zero_y = "disp_at_zero_y"
results_log_stress_at_failure_init = "log_stress_at_failure_init"
results_slope = "slope"
results_final_value = "finalValue"
results_x_at_peak_in_xy = "x_at_peak_in_xy"
results_tabular = "tabular"

label_name = "label"
xEvalStatement = "xEvalStatement"
yEvalStatement = "yEvalStatement"
evalStatement = "evalStatement"

debug(os.getcwd())

# Arguments
jobName = sys.argv[-2]
readOnly = sys.argv[-1] == 'True'

# Load parameters
para = __import__(jobName + '_expected').parameters

# Change working directory to testOutput and put a copy of the input file in testOutput
if jobName + '.odb' not in os.listdir(os.getcwd()):
    os.chdir(os.path.join(os.getcwd(), 'testOutput'))

# Load ODB
odb = session.openOdb(name=os.path.join(os.getcwd(), jobName + '.odb'), readOnly=readOnly)

# Report errors
if odb.diagnosticData.numberOfAnalysisErrors > 0:
    # Ignore excessive element distortion errors when generating failure envelopes
    resultTypes = [r[type_name] for r in para[results_name]]
    if results_log_stress_at_failure_init in resultTypes:
        for e in odb.diagnosticData.analysisErrors:
            if e.description != 'Excessively distorted elements':
                raise Exception("\nERROR: Errors occurred during analysis")
    if "ignoreAnalysisErrors" in para:
        if not para["ignoreAnalysisErrors"]:
            raise Exception("\nERROR: Errors occurred during analysis")
    else:
        raise Exception("\nERROR: Errors occurred during analysis")

# Report warnings
if "ignoreWarnings" in para and not para["ignoreWarnings"]:
    if odb.diagnosticData.numberOfAnalysisWarnings > 0:
        raise Exception("\nERROR: Warnings occurred during analysis")


# Initialize dict to hold the results -- this will be used for assertions in the test_runner
testResults = list()

# Collect results
for iii, r in enumerate(para[results_name]):

    # Get the steps to consider. Default to "Step-1"
    if "step" in r:
        steps = (str(r["step"]), )
    else:
        steps = ("Step-1", )

    # Debugging
    debug(steps)
    debug(r)

    # Collect data from odb for each type of test

    # Max, min
    if r[type_name] in (results_max, results_min):

        # This tries to automatically determine the appropriate position specifier
        varName = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)

        # Get the history data
        identifier_list = [r[identifier_name]]
        if evalStatement in r:
            debug("evalStatement found")
            xy = evaluate_statement(identifier_list=identifier_list, var_names=[varName],
                                    eval_statement=r[evalStatement])
        else:
            debug("evalStatement not found")
            n = str(r[identifier_name][symbol_name]) + '_' + steps[0] + '_' + str(r[type_name])
            xyDataObj = session.XYDataFromHistory(name=n, odb=odb, outputVariableName=varName, steps=steps)
            xy = session.xyDataObjects[n]
        # odb.userData.XYData(n, xy)

        # Get the value calculated in the analysis (last frame must equal to 1, which is total step time)
        if r[type_name] == results_max:
            r[compute_value_name] = max([pt[1] for pt in xy])
        else:
            r[compute_value_name] = min([pt[1] for pt in xy])
        testResults.append(r)

    # Enforce continuity
    elif r[type_name] == results_continous:

        # This trys to automatically determine the appropriate position specifier
        varName = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)

        # Get the history data

        if evalStatement in r:
            xy = evaluate_statement(identifier_list=identifier_list, var_names=[varName],
                                    eval_statement=r[evalStatement])
        else:
            xy = session.XYDataFromHistory(name='XYData-1', odb=odb, outputVariableName=varName, steps=steps)

        # Get the maximum change in the specified value
        r[compute_value_name] = max([max(r[reference_value_name], abs(xy[x][1] - xy[x - 1][1])) for x in range(2, len(xy))])
        testResults.append(r)

    elif r[type_name] == results_xy_infl_pt:
        varNames = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)

        # Get xy data
        x, y = getXY(r, varNames)
        # Combine the x and y data
        xy = combine(x, y)
        tmpName = xy.name
        session.xyDataObjects.changeKey(tmpName, 'ld')
        xy = session.xyDataObjects['ld']
        odb.userData.XYData('ld', xy)

        # Locals
        xyData = xy
        windowMin = r["window"][0]
        windowMax = r["window"][1]

        # Select window
        windowed = [xi for xi in xyData if xi[0] > windowMin and xi[0] < windowMax]
        if len(windowed) == 0:
            raise Exception("No points found in specified window")
        if min([abs(windowed[i][0] - windowed[i - 1][0]) for i in range(1, len(windowed))]) == 0:
            raise "ERROR"
        session.XYData(data=windowed, name="windowed")
        ldWindowed = session.xyDataObjects['windowed']
        odb.userData.XYData('windowed', ldWindowed)

        # Calculate the derivative using a moving window
        xy = differentiate(session.xyDataObjects['windowed'])
        xy = resample(data=xy, numPts=10000)

        # Filter
        if "filterCutOffFreq" in r[identifier_name][1]:
            session.XYData(data=xy, name="temp")
            xy = butterworthFilter(xyData=session.xyDataObjects['temp'], cutoffFrequency=int(r[identifier_name][1]["filterCutOffFreq"]))
            tmpName = xy.name
            session.xyDataObjects.changeKey(tmpName, 'slope')
        else:
            session.XYData(data=xy, name=results_slope)
        slopeXYObj = session.xyDataObjects['slope']
        odb.userData.XYData('slope', slopeXYObj)

        # Differentiate again
        xy = differentiate(session.xyDataObjects['slope'])
        tmpName = xy.name
        session.xyDataObjects.changeKey(tmpName, 'dslope')

        # Get peak from dslope
        dslope = session.xyDataObjects['dslope']
        odb.userData.XYData('dslope', dslope)
        x, y = zip(*dslope)
        y = np.absolute(y)
        dslope = zip(x, y)
        xMax, yMax = max(dslope, key=itemgetter(1))

        # Store the x, y pair at the inflection point
        x, y = zip(*session.xyDataObjects['windowed'])
        r[compute_value_name] = (xMax, interpolate(xMax, x, y))
        testResults.append(r)

    elif r[type_name] == results_disp_at_zero_y:
        varNames = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)

        # Get xy data
        x, y = getXY(r, varNames)
        #y = session.XYDataFromHistory(name=str(r[identifier_name][1][symbol_name]), odb=odb, outputVariableName=varNames[1], steps=steps)
        #x = session.XYDataFromHistory(name=str(r[identifier_name][0][symbol_name]), odb=odb, outputVariableName=varNames[0], steps=steps)
        xy = combine(x, y)

        # Window definition
        if "window" in r:
            windowMin = r["window"][0]
            windowMax = r["window"][1]
        else:
            windowMin = r['referenceValue'] - 2 * r['tolerance']
            windowMax = r['referenceValue'] + 2 * r['tolerance']

        # Use subset of full traction-separation response
        windowed = [xi for xi in xy if xi[0] > windowMin and xi[0] < windowMax]

        # Tolerance to zero
        if "zeroTol" not in r:
            r["zeroTol"] = 1e-6

        # Find pt where stress goes to target
        disp_crit = 0
        for pt in windowed:
            if abs(pt[1]) <= r["zeroTol"]:
                disp_crit = pt[0]
                break

        # Issue error if a value was not found
        if disp_crit == 0:
            raise ValueError("disp_at_zero_y: Could not find a point where y data goes to zero")

        r[compute_value_name] = disp_crit
        testResults.append(r)

    elif r[type_name] == results_log_stress_at_failure_init:

        # Load history data for failure indices and store xy object to list 'failed' if the index is failed
        failed = list()
        varNames = historyOutputNameFromIdentifier(identifier=r["failureIndices"], steps=steps)
        for i in range(0, len(varNames)):

            if evalStatement in r:
                xy = evaluate_statement(identifier_list=identifier_list, var_names=[varName],
                                        eval_statement=r[evalStatement])
            else:
                n = str(r["failureIndices"][i][symbol_name])
                xy = session.XYDataFromHistory(name=n, odb=odb, outputVariableName=varNames[i], steps=steps)
                xy = session.xyDataObjects[n]
            #odb.userData.XYData(n, xy)
            for y in reversed(xy):
                if y[1] >= 1.0:
                    failed.append(xy)
                    break

        # Make sure at least one failure index has reached 1.0
        if len(failed) <= 0:
            raise Exception("No failure occurred in the model")

        # Find the increment number where failure initiated
        i = len(failed[0])
        while True:
            i -= 1
            if len(failed) > 1:
                for fi in failed:
                    if fi[i][1] < 1.0:
                        failed.remove(fi)
                if len(failed) < 1:
                    raise NotImplementedError("Multiple failure modes occurred at the same increment")
                continue
            else:
                if failed[0][i][1] >= 1.0 and failed[0][i - 1][1] < 1.0:
                    inc = i
                    break

        # Get values of stress at final failure
        stressAtFailure = dict()
        varNames = historyOutputNameFromIdentifier(identifier=r["stressComponents"], steps=steps)
        for i in range(0, len(varNames)):
            n = str(r["stressComponents"][i][symbol_name])
            xy = session.XYDataFromHistory(name=n, odb=odb, outputVariableName=varNames[i], steps=steps)
            xy = session.xyDataObjects[n]
            odb.userData.XYData(n, xy)
            stressAtFailure[n] = xy[inc][1]

        # Get additionalIdentifiersToStore data
        additionalData = dict()
        varNames = historyOutputNameFromIdentifier(identifier=r["additionalIdentifiersToStore"], steps=steps)
        for i in range(0, len(varNames)):
            n = str(r["additionalIdentifiersToStore"][i][symbol_name])
            xy = session.XYDataFromHistory(name=n, odb=odb, outputVariableName=varNames[i], steps=steps)
            xy = session.xyDataObjects[n]
            odb.userData.XYData(n, xy)
            additionalData[n] = xy[inc][1]

        # Get job name
        jnparsed = parseJobName(jobName)

        # Format string of data to write
        dataToWrite = jnparsed["loadRatio"] + ', ' + ', '.join([str(x[1]) for x in sorted(stressAtFailure.items(), key=itemgetter(0))])
        dataToWrite += ', ' + ', '.join([str(x[1]) for x in sorted(additionalData.items(), key=itemgetter(0))]) + '\n'

        # Write stresses to file for plotting the failure envelope
        # Write heading if this is the first record in the file
        logFileName = jnparsed["baseName"] + "_" + "failure_envelope.txt"
        if not os.path.isfile(logFileName):
            heading = 'Load Ratio, ' + ', '.join([str(x[0]) for x in sorted(stressAtFailure.items(), key=itemgetter(0))])
            heading += ', ' + ', '.join([str(x[0]) for x in sorted(additionalData.items(), key=itemgetter(0))]) + '\n'
            dataToWrite = heading + dataToWrite
        with open(logFileName, "a") as f:
            f.write(dataToWrite)

    elif r[type_name] == results_slope:
        varNames = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)

        # Get xy data
        x, y = getXY(r, varNames)
        #x = session.XYDataFromHistory(name=str(r[identifier_name][0][symbol_name]), odb=odb, outputVariableName=varNames[0], steps=steps)
        #y = session.XYDataFromHistory(name=str(r[identifier_name][1][symbol_name]), odb=odb, outputVariableName=varNames[1], steps=steps)

        # Combine the x and y data
        xy = combine(x, y)
        tmpName = xy.name
        session.xyDataObjects.changeKey(tmpName, 'slope_xy')
        xy = session.xyDataObjects['slope_xy']
        odb.userData.XYData('slope_xy', xy)

        # Locals
        xyData = xy
        windowMin = r["window"][0]
        windowMax = r["window"][1]

        # Select window
        windowed = [xi for xi in xyData if xi[0] > windowMin and xi[0] < windowMax]
        if len(windowed) == 0:
            raise Exception("No points found in specified window")
        if min([abs(windowed[i][0] - windowed[i - 1][0]) for i in range(1, len(windowed))]) == 0:
            raise "ERROR"
        session.XYData(data=windowed, name="windowed")
        ldWindowed = session.xyDataObjects['windowed']
        odb.userData.XYData('windowed', ldWindowed)

        # Calculate the derivative
        xy = differentiate(session.xyDataObjects['windowed'])
        tmpName = xy.name
        session.xyDataObjects.changeKey(tmpName, 'slope_xy_diff')
        odb.userData.XYData('slope_xy_diff', session.xyDataObjects['slope_xy_diff'])

        # Get the average value of the slope
        x, y = zip(*session.xyDataObjects['slope_xy_diff'])
        r[compute_value_name] = np.mean(y)
        testResults.append(r)

    elif r[type_name] == results_final_value:
        # This trys to automatically determine the appropriate position specifier
        varName = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)

        # Get the history data
        if evalStatement in r:
            xy = evaluate_statement(identifier_list=identifier_list, var_names=[varName],
                                    eval_statement=r[evalStatement])
        else:
            n = str(r[identifier_name][symbol_name]) + '_' + steps[0] + '_' + str(r[type_name])
            xyDataObj = session.XYDataFromHistory(name=n, odb=odb, outputVariableName=varName, steps=steps)
            xy = session.xyDataObjects[n]


        # Get the value calculated in the analysis (last frame must equal to 1, which is total step time)
        r[compute_value_name] = xyDataObj[-1][1]

        testResults.append(r)

    elif r[type_name] == results_x_at_peak_in_xy:
        varNames = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)

        # Get xy data
        x, y = getXY(r, varNames)
        #x = session.XYDataFromHistory(name=str(r[identifier_name][0][symbol_name]), odb=odb, outputVariableName=varNames[0], steps=steps)
        #y = session.XYDataFromHistory(name=str(r[identifier_name][1][symbol_name]), odb=odb, outputVariableName=varNames[1], steps=steps)

        # Combine the x and y data
        xy = combine(x, y)
        tmpName = xy.name
        session.xyDataObjects.changeKey(tmpName, 'ld')
        xy = session.xyDataObjects['ld']
        odb.userData.XYData('ld', xy)

        # Get maximum y value then find corresponding x
        x = [pt[0] for pt in xy]
        y = [abs(pt[1]) for pt in xy]
        y_max = max(y)
        x_at_peak = x[y.index(y_max)]

        # Return
        r[compute_value_name] = x_at_peak
        testResults.append(r)

    elif r[type_name] == results_tabular:
        varNames = historyOutputNameFromIdentifier(identifier=r[identifier_name], steps=steps)


        x, y = getXY(r, varNames)

        # Combine the x and y data
        xy = combine(x, y)
        tmpName = xy.name
        xy_data_name = 'ld{}'.format(iii)
        session.xyDataObjects.changeKey(tmpName, xy_data_name)
        xy = session.xyDataObjects[xy_data_name]
        odb.userData.XYData(xy_data_name, xy)

        xy_np = np.asarray(xy)
        x = xy_np[:, 0]
        y = xy_np[:, 1]

        # Numpy.interp requires x increasing
        if np.all(np.diff(x) < 0):
            raise Exception("x values must be monotonically increasing")

        # Get the y values for the reference x values
        xy_ref_values = r[reference_value_name]
        x_ref_values = [_x for (_x, _) in xy_ref_values]
        y_computed_values = np.interp(x_ref_values, x, y).tolist()
        # list of tuples
        xy_computed_values = [(x, y) for (x, y) in zip(x_ref_values, y_computed_values)]

        # Return
        r[compute_value_name] = xy_computed_values
        testResults.append(r)
    else:
        raise NotImplementedError("test_case result data not recognized: " + str(r))

# Save the odb
if not readOnly:
    debug('Saving xy data')
    odb.save()

# Write the results to a text file for assertions by test_runner
fileName = os.path.join(os.getcwd(), jobName + '_results.py')

# Remove the old results file if it exists
try:
    os.remove(fileName)
except Exception:
    pass

write_results(testResults, fileName)
