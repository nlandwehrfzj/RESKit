from ..NCSource import *

## Define constants
class MerraSource(NCSource):
    
    GWA50_CONTEXT_MEAN_SOURCE   = join(dirname(__file__),"..","..","..","data","gwa50_mean_over_merra.tif")
    GWA100_CONTEXT_MEAN_SOURCE  = join(dirname(__file__),"..","..","..","data","gwa100_mean_over_merra.tif")
    LONG_RUN_AVERAGE_50M_SOURCE = join(dirname(__file__),"..","..","..","data","merra_average_windspeed_50m-shifted.tif")

    MAX_LON_DIFFERENCE=0.3125
    MAX_LAT_DIFFERENCE=0.25

    def __init__(s, source, bounds=None, padFactor=5, **kwargs):
        """Initialize a Merra2 style netCDF4 file source


        Parameters
        ----------
        source : str
            The path to the main data file

        bounds : Anything acceptable to geokit.Extent.load(), optional
            The boundaries of the data which is needed
              * Usage of this will help with memory mangement
              * If None, the full dataset is loaded in memory
              
        padFactor : numeric, optional
            The padding to apply to the boundaries 
              * Useful in case of interpolation
              
        timeName : str, optional
            The name of the time parameter in the netCDF4 dataset
              
        latName : str, optional
            The name of the latitude parameter in the netCDF4 dataset
              
        lonName : str, optional
            The name of the longitude parameter in the netCDF4 dataset

        timeBounds : tuple of length 2, optional
            Used to employ a slice of the time dimension
              * Expect two pandas Timestamp objects> The first indicates the point
                to start collecting data, and the second indicates the end

        """

        NCSource.__init__(s, source=source, bounds=bounds, timeName="time", latName="lat", lonName="lon", 
                          padFactor=padFactor, _maxLonDiff=s.MAX_LON_DIFFERENCE, _maxLatDiff=s.MAX_LAT_DIFFERENCE,
                          **kwargs)
        s.timeindex=pd.Index(s.timeindex, tz="GMT")

    def __add__(s,o):
        out = MerraSource(None)
        return NCSource.__add__(s, o, _shell=out)

    def contextAreaAtIndex(s, latI, lonI):
        """Compute the context area surrounding the a specified index"""
        # Make and return a box
        lowLat = s.lats[latI]-0.25
        highLat = s.lats[latI]+0.25
        lowLon = s.lons[lonI]-0.3125
        highLon = s.lons[lonI]+0.3125
        
        return gk.geom.box( lowLon, lowLat, highLon, highLat, srs=gk.srs.EPSG4326 )

    def loadWindSpeed(s, height=50, winddir=False ):
        """Load the U and V wind speed data at the specified height, and compute
        the overall windspeed and winddir

        Parameters
        ----------
        height : int, optional
            The height value to load, given in meters above ground
              * Options are 2, 10, 50
              * Maps to a vaiable named 'windspeed'

        winddir : bool, optional
            If True, the wind direction is calculated and saved under a variable
            named 'winddir'
        """

        # read raw data
        s.load("U%dM"%height)
        s.load("V%dM"%height)

        # read the data
        uData = s.data["U%dM"%height]
        vData = s.data["V%dM"%height]

        # combine into a single time series matrix
        speed = np.sqrt(uData*uData+vData*vData) # total speed
        s.data["windspeed"] = speed

        if winddir: 
            direction = np.arctan2(vData,uData)*(180/np.pi)# total direction
            s.data["winddir"] = direction

    def loadRadiation(s):
        """Load the SWGNT and SWGDN variables into the data table with names
        'ghi' and 'dni' respectively
        """
        s.load("SWGNT", name="ghi")
        s.load("SWGDN", name="dni")

    def loadTemperature(s, which='air', height=2):
        """Load air temperature variables 
        
        The name of the variable loaded into the data table depends on the type
        of temperature chosen:
          * If which='air' -> 'air_temp' is created
          * If which='dew' -> 'dew_temp' is created
          * If which='wet' -> 'wet_temp' is created

        Parameters:
        -----------
        which : str, optional
            The specific type of air temperature to read
            * Can be: air, dew, or wet

        height : int, optional
            The height in meters to load
            * Options are: 2, 10? and 50?
        """
        if which.lower() == 'air': varName = "T%dM"%height
        elif which.lower() == 'dew': varName = "T%dMDEW"%height
        elif which.lower() == 'wet': varName = "T%dMWET"%height
        else: raise ResMerraError("sub group '%s' not understood"%which)

        # load
        s.load(varName, name=which+"_temp", processor=None)

    def loadPressure(s): 
        """Load the PS Merra variable into the data table with the name 'pressure'"""
        s.load("PS", name='pressure')

    def loadSet_PV(s):
        """Load basic PV power simulation variables
        
          * 'windspeed' from U2M and V2M
          * 'ghi' from SWGNT 
          * 'dni' from SWGDN 
          * 'air_temp' from T2M
          * 'pressure' from PS
        """
        s.loadWindSpeed(height=2)
        del s.data["U2M"]
        del s.data["V2M"]

        s.loadRadiation()
        s.loadTemperature('air', height=2)
        s.loadPressure()

    def loadSet_Wind(s):
        """Load basic Wind power simulation variables
        
          * 'windspeed' from U50M and V50M
        """
        s.loadWindSpeed(height=50)
        del s.data["U50M"]
        del s.data["V50M"]
