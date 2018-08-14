import numpy as np
import pandas as pd
from collections import namedtuple, OrderedDict
from res.util import *
from res.weather import NCSource
import pvlib
from types import FunctionType
from datetime import datetime as dt

class _SolarLibrary:
    def __init__(s):
        s._cecmods = None
        s._sandiamods = None
        s._cecinverters = None
        s._sandiainverters = None

    def modules(s, group='cec'): 
        name = "_"+group.lower()+"mods"
        if getattr(s, name) is None:
            setattr(s, name, pvlib.pvsystem.retrieve_sam(group+"mod"))
        return getattr(s, name)

    def inverters(s, group='sandia'): 
        name = "_"+group.lower()+"inverters"
        if getattr(s, name) is None:
            setattr(s, name, pvlib.pvsystem.retrieve_sam(group+"inverter"))
        return getattr(s, name)
    
SolarLibrary = _SolarLibrary()

def _sapm_celltemp(poa_global, wind_speed, temp_air, model='open_rack_cell_glassback'):
    """ Cell temp function updated from PVLIB version! """
    temp_models = {'open_rack_cell_glassback': [-3.47, -.0594, 3],
                   'roof_mount_cell_glassback': [-2.98, -.0471, 1],
                   'open_rack_cell_polymerback': [-3.56, -.0750, 3],
                   'insulated_back_polymerback': [-2.81, -.0455, 0],
                   'open_rack_polymer_thinfilm_steel': [-3.58, -.113, 3],
                   '22x_concentrator_tracker': [-3.23, -.130, 13]
                   }

    if isinstance(model, str):
        model = temp_models[model.lower()]
    elif isinstance(model, list):
        model = model
    elif isinstance(model, (dict, pd.Series)):
        model = [model['a'], model['b'], model['deltaT']]

    a = model[0]
    b = model[1]
    deltaT = model[2]

    E0 = 1000.  # Reference irradiance

    temp_module = poa_global*np.exp(a + b*wind_speed) + temp_air

    temp_cell = temp_module + (poa_global / E0)*(deltaT)

    return temp_cell


def simulatePVModule(locs, source, elev=300, module="SunPower_SPR_X21_255", azimuth=180, tilt="latitude", totalSystemCapacity=None, tracking="fixed", modulesPerString=1, inverter=None, stringsPerInverter=1, rackingModel='open_rack_cell_glassback', airMassModel='kastenyoung1989', transpositionModel='haydavies', cellTempModel="sandia", generationModel="single-diode", inverterModel="sandia", interpolation="bilinear", loss=0.00, trackingGCR=2/7, trackingMaxAngle=60, frankCorrection=False):
    """
    Performs a simple PV simulation

    For module options, see: res.solarpower.ModuleLibrary
    For inverter options, see: res.solarpower.InverterLibrary

    interpolation options are:
        - near
        - bilinear
        - cubic
    """
    # start = dt.now()
    # timings = [0, ]
    # timingNames = ["start", ]
    # def addTime(name, full=False):
    #     tmp = (dt.now()-start).total_seconds()
    #     if not full: tmp -= sum(timings)
    #     timings.append(tmp)
    #     timingNames.append(name)

    ### Check a few inputs so it doesn't need to be done repeatedly
    if cellTempModel.lower() == "sandia": sandiaCellTemp = True
    elif cellTempModel.lower() == "noct": sandiaCellTemp = False
    else: raise ValueError("cellTempModel parameter not understood")

    if generationModel.lower() == "sapm": sandiaGenerationModel = True
    elif generationModel.lower() == "single-diode": sandiaGenerationModel = False
    else: raise ValueError("generationModel parameter not understood")

    if inverterModel.lower() == "sandia": sandiaInverterModel = True
    elif inverterModel.lower() == "driesse": sandiaInverterModel = False
    else: raise ValueError("generationModel parameter not understood")

    if tracking.lower() == "single-axis": singleAxis = True
    elif tracking.lower() == "fixed": singleAxis = False
    else: raise ValueError("tracking parameter not understood")

    #addTime("param check")

    ### Normalize location
    locs = LocationSet(locs)
    #addTime("arrange locations")

    ### Collect weather data
    if isinstance(source, NCSource):
        times = source.timeindex

        idx = source.loc2Index(locs, asInt=False)
        k = dict( locations=locs, interpolation=interpolation, forceDataFrame=True, _indicies=idx )

        ghi = source.get("ghi", **k)
        dhi = source.get("dhi", **k) if "dhi" in source.data else None
        dni = source.get("dni", **k) if "dni" in source.data else None

        windspeed = source.get("windspeed", **k)
        pressure = source.get("pressure", **k)
        air_temp = source.get("air_temp", **k)
        dew_temp = None
        if "albedo" in source.data: 
            albedo = source.get("albedo", **k)
        else: 
            albedo = 0.2

    else: # source should be a dictionary
        times = source["times"]
        
        ghi = source["ghi"]
        dhi = source["dhi"] if "dhi" in source else None
        dni = source["dni"] if "dni" in source else None

        windspeed = source["windspeed"]
        pressure = source["pressure"]
        air_temp = source["air_temp"]
        dew_temp = None
        if "albedo" in source: 
            albedo = source["albedo"]
        else: 
            albedo = 0.2

    #addTime("Extract weather data")

    ### Identify module and inverter
    if isinstance(module, str):
        if sandiaGenerationModel: 
            module = SolarLibrary.modules("sandia")[module] # Extract module parameters
            moduleCap = module.Impo*module.Vmpo # Max capacity of a single module
        elif not sandiaGenerationModel: 
            module = SolarLibrary.modules("cec")[module] # Extract module parameters
            moduleCap = module.I_mp_ref*module.V_mp_ref # Max capacity of a single module

            ## Check if we need to add the Desoto parameters
            # defaults for EgRef and dEgdT taken from the note in the docstring for 
            #  'pvlib.pvsystem.calcparams_desoto'
            if not "EgRef" in module: module['EgRef'] =  1.121
            if not "dEgdT" in module: module['dEgdT'] = -0.0002677

    if not inverter is None and isinstance(inverter, str):
        try:
            inverter = SolarLibrary.inverters("cec")[inverter]
        except KeyError:
            inverter = SolarLibrary.inverters("sandia")[inverter]
        else:
            raise KeyError("Could not load an inverter with name: "+inverter)

    ### Construct Generic system (tilt and azimuth given at sim time)
    if singleAxis:
        genericSystem = pvlib.tracking.SingleAxisTracker(axis_tilt=None, axis_azimuth=None, 
                            max_angle=trackingMaxAngle, module_parameters=module, albedo=albedo, 
                            modules_per_string=modulesPerString, strings_per_inverter=stringsPerInverter, 
                            inverter_parameters=inverter, racking_model=rackingModel, gcr=trackingGCR)
    else:
        genericSystem = pvlib.pvsystem.PVSystem(surface_tilt=None, surface_azimuth=None, 
                            module_parameters=module, albedo=albedo, modules_per_string=modulesPerString, 
                            strings_per_inverter=stringsPerInverter, inverter_parameters=inverter, 
                            racking_model=rackingModel)
    #addTime("Create generic module")

    ### Check the (potentially) uniquely defined inputs
    if not totalSystemCapacity is None:
        if isinstance(totalSystemCapacity, pd.Series): pass
        elif isinstance(totalSystemCapacity, float) or isinstance(totalSystemCapacity, int):
            totalSystemCapacity = pd.Series([totalSystemCapacity,]*locs.count, index=locs)
        else:
            totalSystemCapacity = pd.Series(totalSystemCapacity, index=locs)

    if isinstance(azimuth, pd.Series): pass
    elif isinstance(azimuth, float) or isinstance(azimuth, int):
        azimuth = pd.Series([azimuth,]*locs.count, index=locs)
    else:
        azimuth = pd.Series(azimuth, index=locs)

    if isinstance(tilt, pd.Series): pass
    if isinstance(tilt,str):
        if tilt=="latitude": tilt=pd.Series([l.lat for l in locs], index=locs)
        elif tilt=="half-latitude": tilt=pd.Series([l.lat/2 for l in locs], index=locs)
        else: raise ValueError("tilt directive '%s' not recognized"%tilt)
    elif isinstance(tilt, FunctionType):
        tilt = tilt=pd.Series([tilt(l,e) for l,e in zip(locs, elev)], index=locs)
    elif isinstance(tilt, float) or isinstance(tilt, int):
        tilt = pd.Series([tilt,]*locs.count, index=locs)
    else:
        tilt = pd.Series(tilt, index=locs)

    if elev is None: raise ValueError("elev cannot be None")
    elif isinstance(elev, pd.Series): pass
    elif isinstance(elev, str): 
        elev = gk.raster.extractValues(elev, locs).data
    elif isinstance(elev, float) or isinstance(elev, int):
        elev = pd.Series([elev,]*locs.count, index=locs)
    else:
        elev = pd.Series(elev, index=locs)

    #addTime("Check unique inputs")

    ### Begin simulations
    # get solar position
    _solpos = {}
    checkedSolPosValues = {}

    for i,loc in enumerate(locs):
        solposLat = np.round(loc.lat, 1) # Only perform solor position calc for every .1 degrees
        solposLon = np.round(loc.lon, 1) # Only perform solor position calc for every .1 degrees
        solposElev = np.round(elev[loc], -2) # Only perform solor position calc for every 100 meters
        if isinstance(solposElev, pd.Series): raise ResError("%s is not unique"%str(loc))

        solposKey = (solposLon, solposLat, solposElev)
        if not solposKey in checkedSolPosValues:
            # Pressure and temperature should not change much between evaluated locations
            checkedSolPosValues[solposKey] = pvlib.solarposition.spa_python(times, 
                                                                            latitude=solposLat,
                                                                            longitude=solposLon,
                                                                            altitude=solposElev,
                                                                            pressure=pressure[loc],  
                                                                            temperature=air_temp[loc])

        _solpos[loc] = checkedSolPosValues[solposKey]
    solpos = {}
    for c in ['apparent_zenith', 'azimuth', 'apparent_elevation']:
        solpos[c] = pd.DataFrame(np.column_stack([_solpos[loc][c].copy() for loc in locs]), columns=locs, index=times)
    del _solpos, checkedSolPosValues
    #addTime("Solar position")

    # DNI Extraterrestrial
    dni_extra = pvlib.irradiance.extraradiation(times).values
    if len(ghi.shape) > 1:
        dni_extra = np.broadcast_to(dni_extra.reshape((ghi.shape[0],1)), ghi.shape)
    #addTime("DNI Extra")
    
    # Apply Frank corrections when dealing with COSMO data?
    if frankCorrection:
        transmissivity = ghi/dni_extra
        sigmoid = 1/(1+np.exp( -(transmissivity-0.5)/0.03 ))

        # Adjust cloudy regime
        months = times.month
        cloudyFactors = np.empty(months.shape)

        cloudyFactors[months==1] = 0.7776553729824053
        cloudyFactors[months==2] = 0.7897164461247639
        cloudyFactors[months==3] = 0.8176553729824052
        cloudyFactors[months==4] = 0.8406805293005672
        cloudyFactors[months==5] = 0.8761808928311765
        cloudyFactors[months==6] = 0.9094139886578452
        cloudyFactors[months==7] = 0.9350856478115459
        cloudyFactors[months==8] = 0.9191682419659737
        cloudyFactors[months==9] = 0.912703795259561
        cloudyFactors[months==10]= 0.8775035625999711
        cloudyFactors[months==11]= 0.8283158353933402
        cloudyFactors[months==12]= 0.7651417769376183
        cloudyFactors = np.broadcast_to(cloudyFactors.reshape( (cloudyFactors.size,1) ), ghi.shape)

        cloudyFactors = cloudyFactors*(1-sigmoid)

        # Adjust clearsky regime
        e = solpos["apparent_elevation"]
        clearSkyFactors = np.ones(e.shape)


        clearSkyFactors[np.where((e>=10)&(e<20))] = 1.17612920884004
        clearSkyFactors[np.where((e>=20)&(e<30))] = 1.1384180020822825
        clearSkyFactors[np.where((e>=30)&(e<40))] = 1.1022951259566156
        clearSkyFactors[np.where((e>=40)&(e<50))] = 1.0856852748290704
        clearSkyFactors[np.where((e>=50)&(e<60))] = 1.0779254457050245
        clearSkyFactors[np.where(e>=60)] = 1.0715262914980628

        clearSkyFactors *= sigmoid

        # Apply to ghi
        totalCorrectionFactor = clearSkyFactors+cloudyFactors
        ghi *= totalCorrectionFactor

        del clearSkyFactors, cloudyFactors, totalCorrectionFactor, e, months, sigmoid, transmissivity
        #addTime("Frank Correction")

    # Compute DHI or DNI
    if dni is None:
        dni = pd.DataFrame(0, index=times, columns=ghi.columns)
        for c in ghi.columns:
            dni[c] = pvlib.irradiance.dirint(ghi[c], solpos["apparent_zenith"][c], times, pressure=pressure[c], 
                                             temp_dew=None if dew_temp is None else dew_temp[c] )

    if dhi is None:
        dhi = ghi - dni*np.sin( np.radians(solpos["apparent_elevation"])) # TODO: CHECK THIS!!!
        dhi[dhi<0] = 0

    #addTime("DHI calc")
    
    # Get tilt and azimuths
    if singleAxis:
        axis_tilt = tilt
        axis_azimuth = azimuth

        tilt = pd.DataFrame(index=times, columns=locs)
        azimuth = pd.DataFrame(index=times, columns=locs)

        for loc, at, aa in zip(locs, axis_tilt, axis_azimuth):
            tmp = pvlib.tracking.singleaxis(apparent_zenith=solpos["apparent_zenith"][loc], 
                                            apparent_azimuth=solpos["azimuth"][loc], axis_tilt=at, axis_azimuth=aa, 
                                            max_angle=genericSystem.max_angle, backtrack=genericSystem.backtrack,
                                            gcr=genericSystem.gcr)
            tilt[loc] = tmp["surface_tilt"].copy()
            azimuth[loc] = tmp["surface_azimuth"].copy()

        del axis_azimuth, axis_tilt, tmp

    # Airmass
    if sandiaGenerationModel: # airmass only needed for SAPM model
        amRel = pvlib.atmosphere.relativeairmass(solpos["apparent_zenith"])
        amAbs = pvlib.atmosphere.absoluteairmass(amRel, pressure)
        del amRel
    #addTime("Airmass")
    

    # Angle of Incidence
    if sandiaGenerationModel: # aoi only needed for SAPM model
        aoi = pvlib.irradiance.aoi(tilt, azimuth, solpos['apparent_zenith'], solpos['azimuth'])
    #addTime("AOI")
    

    # Compute Total irradiation
    poa = pvlib.irradiance.total_irrad(tilt,
                                       azimuth,
                                       solpos['apparent_zenith'],
                                       solpos['azimuth'],
                                       dni, ghi, dhi,
                                       dni_extra=dni_extra,
                                       model='haydavies')
    del dni, ghi, dhi, tilt, azimuth, dni_extra, solpos
    #addTime("POA")
    

    # Cell temp
    if sandiaCellTemp:
        cellTemp = _sapm_celltemp(poa['poa_global'], windspeed, air_temp)
    else:
        # TODO: Implement NOCT model
        raise ResError("NOCT celltemp module not yet implemented :(")
    del air_temp, windspeed
    #addTime("Cell temp")
    

    ## Add irradiance losses
    # TODO: Look into this. Is it necessary???
    # See pvsystem.physicaliam, pvsystem.ashraeiam, and pvsystem.sapm_aoi_loss

    ## Do DC Generation calculation
    if sandiaGenerationModel:
        # Not guarenteed to work with 2D inputs
        effectiveIrradiance = pvlib.pvsystem.sapm_effective_irradiance( poa_direct=poa['poa_direct'], 
                                    poa_diffuse=poa['poa_diffuse'], airmass_absolute=amAbs, aoi=aoi)
        rawDCGeneration = pvlib.pvsystem.sapm(effective_irradiance=effectiveIrradiance, 
                                   temp_cell=moduleTemp['temp_cell'])
        del amAbs, aoi
    else:
        sel = (poa["poa_global"]>0).values
        sotoParams = pvlib.pvsystem.calcparams_desoto(poa_global=poa["poa_global"].values[sel], 
                                                      temp_cell=cellTemp.values[sel], 
                                                      alpha_isc=module.alpha_sc, 
                                                      module_parameters=module, 
                                                      EgRef=module.EgRef, 
                                                      dEgdT=module.dEgdT)

        photoCur, satCur, resSeries, resShunt, nNsVth = sotoParams
        
        tmp = pvlib.pvsystem.singlediode(photocurrent=photoCur, saturation_current=satCur, 
                                         resistance_series=resSeries, resistance_shunt=resShunt, 
                                         nNsVth=nNsVth)
        
        rawDCGeneration = OrderedDict()
        for k in ["p_mp", "v_mp", ]:
            rawDCGeneration[k] = pd.DataFrame(index=times, columns=locs)
            rawDCGeneration[k].values[sel] = tmp[k]

        del photoCur, satCur, resSeries, resShunt, nNsVth, tmp
        
    del poa, cellTemp
    #addTime("DC Sim")

    ## Simulate inverter interation
    if not inverter is None: 
        if sandiaInverterModel: 
            generation = genericSystem.snlinverter(v_dc=rawDCGeneration['v_mp']*system.modules_per_string, 
                                                   p_dc=rawDCGeneration['p_mp']*system.modules_per_string*system.strings_per_inverter)

        else: 
            generation = genericSystem.adrinverter(v_dc=rawDCGeneration['v_mp']*system.modules_per_string, 
                                                   p_dc=rawDCGeneration['p_mp']*system.modules_per_string*system.strings_per_inverter)
        
        # normalize to a single module
        generation = generation/system.modules_per_string/system.strings_per_inverter
    else:
        generation = rawDCGeneration["p_mp"].copy()
    del rawDCGeneration
    #addTime("Inverter Sim")

    generation *= (1-loss) # apply a flat loss

    if totalSystemCapacity is None:
        output = generation/moduleCap # outputs in capacity-factor
    else:
        # output in whatever unit totalSystemCapacity is in
        output = generation/moduleCap*totalSystemCapacity

    #addTime("Finalize")

    # Done!
    #addTime("total",True)
    return output.fillna(0)
    
def simulatePVRooftopDistribution(locs, tilts, azimuths, probabilityDensity, rackingModel="roof_mount_cell_glassback", **kwargs):
    """
    Simulate a distribution of pv rooftops and combine results
    """
    output = None
    for ti, tilt in enumerate(tilts):
        output.append([])
        for ai, azimuth in enumerate(tilts):
            simResult = simulatePVModule(locs=locs, tilt=tilt, azimuth=azimuth, 
                                         rackingModel=rackingModel, extract=extract, **kwargs)

            if output is None: output=simResult*probabilityDensity[ti,ai]
            else: finalResult += simResult*probabilityDensity[ti,ai]
    return finalResult