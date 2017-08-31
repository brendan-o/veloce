"""
Control the servo loop for Veloce temperature control. This runs as a server that
accepts text or ZMQ-based input, and runs the servo loop as a background task.

Version history
---------------
0.1     19 Jan 2017     MJI     Skeleton only
0.2     July 2017       MR/MJI  Functioning with test plate
0.3     29 Aug 2017     MJI     Tidy-up, especially logging
"""
from __future__ import print_function, division
import numpy as np
from labjack import ljm
import time
import numpy as np
import matplotlib.pyplot as plt
from lqg_math import *
import logging

LABJACK_IP = "150.203.91.171"
#Long sides, short sides, lid and base for FIO 0,2,3,4 respectively.
#NB: Heaters checked at 30, 30, 20 and 20 ohms.
HEATER_LABELS = ["Long", "Short", "Lid", "Base", "Cryostat"]
HEATER_DIOS = ["0","2","3","4","5"]         #Input/output indices of the heaters
PWM_MAX =1000000
#Table, lower then upper.
AIN_LABELS = ["Table", "Lower", "Upper"]
AIN_NAMES = ["AIN0", "AIN2", "AIN4"] #Temperature analog input names
HEATER_MAX = 3.409
LJ_REST_TIME = 0.01
TEMP_DERIV = 0.00035 #K/s with heater on full.
PID_GAIN_HZ = 0.002
LOG_FILENAME = 'thermal_control.log'
#
Set the following to logging.INFO on
logging.basicConfig(filename=LOG_FILENAME, level=logging.DEBUG, \
    format='%(asctime)s, %(created)f, %(levelname)s,  %(message)s', \
    datefmt='%Y-%m-%d %H:%M:%S')

class ThermalControl:
    def __init__(self, ip=None):
        if not ip:
            self.ip = LABJACK_IP
        try:
            self.handle = ljm.openS("ANY", "ANY", self.ip)
        except:
            print("Unable to open labjack {}".format(self.ip))
            self.labjack_open=False
            return
        self.labjack_open=True
        #WARNING: This really should read from the labjack, and set the heater values
        #appropriately
        self.cmd_initialize("")
        self.voltages=99.9*np.ones(len(AIN_NAMES))
        self.lqg=False
        self.use_lqg=True
        self.storedata = False
        self.setpoint = 25.0
        self.nreads=int(0)
        self.last_print=-1
        self.ulqg = 0
        self.lqgverbose = False
        self.x_est = np.array([[0.], [0.], [0.]])
        self.u = np.array([[0]])
        #PID Constants
        self.pid=False
        self.pid_gain = PID_GAIN_HZ/TEMP_DERIV
        self.pid_i = 0.5*PID_GAIN_HZ**2/TEMP_DERIV
        self.pid_ints = np.array([0,0])
        

    #Our user or socket commands
    def cmd_initialize(self, the_command):
        """Set the heaters to zero, and set the local copy of the heater values
        (in the range 0-1) to 0. 
        
        By default, we use a 10 MHz clock (divisor of 8) and count up to 1,000,000 to give
        PWM at a 10Hz rate.
        """
        if not self.labjack_open:
            raise UserWarning("Labjack not open!")
            
        #Set FIO 0,2,3 as an example. Note that FIO 1 isn't allowed to have PWM:
        #https://labjack.com/support/datasheets/t7/digital-io/extended-features
        #(and FIO4 upwards is only available via one of the DB connectors)
        self.current_heaters = np.zeros(len(HEATER_DIOS))
        aNames = ["DIO_EF_CLOCK0_DIVISOR", "DIO_EF_CLOCK0_ROLL_VALUE", "DIO_EF_CLOCK0_ENABLE"]
        #Set the first number below to 256 for testing on a multimeter.
        #Set to 8 for normal operation
        aValues = [8, PWM_MAX, 1]
        for dio in HEATER_DIOS:
            aNames.extend(["DIO"+dio+"_EF_INDEX", "DIO"+dio+"_EF_CONFIG_A", "DIO"+dio+"_EF_ENABLE"])
            aValues.extend([0,0,1])

        #See labjack example python scripts - looks pretty simple!
        numFrames = len(aNames)
        results = ljm.eWriteNames(self.handle, numFrames, aNames, aValues)
        
        #Now set up the Analog input
        #Note that the settling time is set to default (0), which isn't actually
        #zero microsecs.
        aNames = ["AIN_ALL_NEGATIVE_CH", "AIN_ALL_RANGE", "AIN_ALL_RESOLUTION_INDEX", "AIN_ALL_SETTLING_US"]
        aValues = [1, 1, 10, 0]
        numFrames = len(aNames)
        results = ljm.eWriteNames(self.handle, numFrames, aNames, aValues)
        
    def cmd_close(self, the_command):
        """Close the connection to the labjack"""
        ljm.close(self.handle)
        self.labjack_open=True
        return "Labjack connection closed."
        
    def cmd_heater(self, the_command):
        """Set a single heater to a single PWM output"""
        the_command = the_command.split()
        #Error check the input (for bugshooting, do this
        #even if the labjack isn't open)
        if len(the_command) != 3:
            return "Useage: HEATER [heater_index] [fraction]"
        else:
            try:
                dio_index = int(the_command[1])
            except:
                return "ERROR: heater_index must be an integer"
            try:
                dio = HEATER_DIOS[dio_index]
            except:
                return "ERROR: heater_index out of range"
            try:
                fraction = float(the_command[2])
            except:
                return "ERROR: fraction must be between 0.0 and 1.0"
            if (fraction < 0) or (fraction > 1):
                return "ERROR: fraction must be between 0.0 and 1.0"
        #Check that the labjack is open
        if not self.labjack_open:
            raise UserWarning("Labjack not open!")
                
        #Now we've error-checked, we can turn the heater fraction to an
        #integer and write tot he labjack
        aNames = ["DIO"+dio+"_EF_CONFIG_A"]
        aValues = [int(fraction * PWM_MAX)]
        numFrames = len(aNames)
        results = ljm.eWriteNames(self.handle, numFrames, aNames, aValues)
        return "Done."
        
    def cmd_setgain(self, the_command):
        """Dummy gain setting function"""
        the_command = the_command.split()
        if len(the_command)!=2:
            return "Useage: SETGAIN [newgain]"
        else:
            return "Gain not set to {}".format(the_command[1:])
            
    def cmd_getvs(self, the_command):
        """Return the current voltages as a string.
        """
        #FIXME: Return an arbitrary number of voltages.
        return "{0:9.6f} {1:9.6f} {2:9.6f}".format(self.voltages[0], self.voltages[1], self.voltages[2])

    def cmd_gettemp(self, the_command):
        """Return the temperature to the client as a string"""
        temps = self.gettemps()
        return (', {:9.6f}'*len(AIN_NAMES)).format(*temps)[2:]
        #return "{0:9.6f}, {0:9.6f} {0:9.6f}".format(self.gettemp(0), self.gettemp(1), self.gettemp(2))

    def cmd_lqgstart(self, the_command):
        self.lqg = True
    
    def cmd_lqgsilent(self, the_command):
        self.lqgverbose = False
        return ""
    
    def cmd_lqgverbose(self, the_command):
        self.lqgverbose = True
        return ""

    def cmd_startrec(self, the_command):
        self.storedata = True
        return ""

    def cmd_stoprec(self, the_command):
        self.storedata = False
        return ""

    def cmd_lqgstop(self, the_command):
        self.lqg = False 
        return ""

    def cmd_pidstart(self, the_command):
        self.pid = True
        return ""

    def cmd_pidstop(self, the_command):
        self.pid = False
        return ""

    def set_heater(self, ix, fraction):
        """Set the heater to a fraction of its full range.
        
        Parameters
        ----------
        ix: int
            The heater to set.
        
        fraction: float
            The fractional heater current (via PWM).
        """
        aNames = ["DIO"+HEATER_DIOS[ix]+"_EF_CONFIG_A"]
        aValues = [int(fraction * PWM_MAX)]
        numFrames = len(aNames)
        results = ljm.eWriteNames(self.handle, numFrames, aNames, aValues)

    def gettemp(self, ix, invert_voltage=True):
        """Return one temperature as a float. See Roberton's the
        
        v_out = v_in * [ R_t/(R_T + R) - R/(R_T + R) ]
        R_T - R = (R_T + R) * (v_out/v_in)
        R_T*(1 - (v_out/v_in)) = R * (1 + (v_out/v_in))
        R_T = R * (v_in + v_out) / (v_in - v_out)
        
        Parameters
        ----------
        ix: int
            Index of the sensor to be provided.
            
        Returns
        -------
        temp: float
            Temperature in Celcius
        """
        ##uses converts voltage temperature, resistance implemented
        R = 10000
        Vin = 5
        if invert_voltage:
            voltage = -self.voltages[ix]
        else:
            voltage = self.voltages[ix]
        resistance = R * (Vin + voltage)/(Vin - voltage)
        #resistance = (2*R*self.voltage)/(Vin - self.voltage)
        #resistance += R
        aVal = 0.00113259149597421
        bVal = 0.000233514798680064
        cVal = 0.00000009045521729374
        temp_inv = aVal + bVal*np.log(resistance) + cVal*((np.log(resistance))**3)
        tempKelv = 1/(temp_inv)
        tempCelc = tempKelv -273.15
        return tempCelc
        
    def gettemps(self):
        """Get all temperatures.
        
        Returns
        -------
        temps: list
            A list of temperatures for all sensors.
        """
        temps = ()
        for ix in range(len(AIN_NAMES)):
            temps += (self.gettemp(ix),)
        return temps

    def job_doservo(self):
        """Servo loop job
        
        Just read the voltage, and print once per second 
        (plus the number of reads)"""
        time.sleep(lqg_dt) #!!! MJI should be lqg_math.dt
        for ix, ain_name in enumerate(AIN_NAMES):
            try:
                self.voltages[ix] = ljm.eReadName(self.handle, ain_name)
            except:
                print("Could not read temperature {:d} one time".format(ix))
                logging.warning("Could not read temperature {:d} one time".format(ix))
                #Now try again
                try:
                    self.voltages[ix] = ljm.eReadName(self.handle, ain_name)
                except:
                    print("Giving up reading temperature {:d}".format(ix))
                    logging.error("Giving up reading temperature {:d}".format(ix))
        self.nreads += 1
        if time.time() > self.last_print + 1:
            self.last_print=time.time()
        #    print("Voltage: {0:9.6f}".format(self.voltage))
      
        #Get the current temperature and set it to .
        tempnow = self.gettemp(0)
  
        if self.lqg:
            #!!! MATTHEW - this next line is great for debugging !!!
            #!!! Uncomment it to look at variables, e.g. print(y)
            #!!! and y.shape
            #import pdb; pdb.set_trace()
            
            #Store the current temperature in y.
            y = np.zeros( (1,1) )
            y[0] = tempnow - self.setpoint
            
            #Based on this measurement, what is the next value of x_i+1 est?
            x_est_new = np.dot(A_mat, self.x_est)
            x_est_new += np.dot(B_mat, self.u)
            dummy = y - np.dot(C_mat, (np.dot(A_mat, self.x_est) + np.dot(B_mat, self.u)))
            x_est_new += np.dot(K_mat, dummy)
            self.x_est = x_est_new #x_i+1 has now become xi
            # Now find u
            if self.use_lqg:
                self.u = -np.dot(L_mat, self.x_est)
                self.ulqg = self.u[0,0]
                #offset because heater can't be negative
                fraction = self.u[0,0]/HEATER_MAX

                if fraction < 0:
                    self.u[0,0] = 0
                    fraction = 0
                elif fraction > 1:
                    self.u[0,0] = HEATER_MAX
                    fraction = 1
              
                
                #fraction = 0
                #FIXME: This assumes we are using heater 0.
                aNames = ["DIO"+HEATER_DIOS[0]+"_EF_CONFIG_A"]
                aValues = [int(fraction * PWM_MAX)]
                numFrames = len(aNames)
                results = ljm.eWriteNames(self.handle, numFrames, aNames, aValues)

            #!!!Another error here, u was an array, so numpy prints it as a string
            if self.lqgverbose == 1:
                print("Heater Wattage: {0:9.6f}".format(self.u[0,0]))
                print("Heater Fraction: {0:9.6f}".format(fraction))
                print("Ambient Temperature {:9.4f}".format(self.x_est[0,0] + self.setpoint))
                #print("Heater Temperature {:9.4f}".format(self.x_est[1,0] + self.setpoint))
               # print("Plate Temperature {:9.4f}".format(self.x_est[2,0] + self.setpoint))
                tempsensor = -1*self.x_est[0,0]*G_sa/(G_sa+G_ps)+self.x_est[2,0]*G_ps/(G_ps+G_sa)
                print("Estimated sensor Temperature {:9.4f}".format(tempsensor + self.setpoint))
                print(self.ulqg)
        elif self.pid:
            #Start the PID loop. For the integral component, reset whenever the heater 
            #hits the rail.
            t0 = self.gettemp(1)
            self.pid_ints[0] += lqg_dt*t0
            h0 = 0.5 + self.pid_gain*(self.setpoint - t0) + self.pid_i*self.pid_ints[0]
            if (h0<0):
                h0=0
                self.pid_ints[0]=0
            if (h0>1):
                h0=1
                self.pid_ints[0]=0
            t1 = self.gettemp(2)
            self.pid_ints[0] += lqg_dt*t1
            h1 = 0.5 + self.pid_gain*(self.setpoint - t1) + self.pid_i*self.pid_ints[1]
            if (h1<0):
                h1=0
                self.pid_ints[1]=0
            if (h1>1):
                h1=1
                self.pid_ints[1]=0
            #Now control the heaters...
            self.set_heater(0, h1)
            self.set_heater(1, h1)
            self.set_heater(2, h1)
            self.set_heater(3, h0)

        if self.lqgverbose == 1:
            print("---")
            print("Table Temperature: {0:9.6f}".format(self.gettemp(0)))
            print("Lower Temperature: {0:9.6f}".format(self.gettemp(1)))
            print("Upper Temperature: {0:9.6f}".format(self.gettemp(2)))
            
        if self.storedata:
            logging.info('TEMPS, ' + self.cmd_gettemp(""))
            #logging.info('TEMPS' + ', {:9.6f}'*len(AIN_NAMES).format())
            
        return 
