'''
DISTRIBUTION STATEMENT A. Approved for public release: distribution unlimited.

This material is based upon work supported by the Assistant Secretary of Defense for 
Research and Engineering under Air Force Contract No. FA8721-05-C-0002 and/or 
FA8702-15-D-0001. Any opinions, findings, conclusions or recommendations expressed in this
material are those of the author(s) and do not necessarily reflect the views of the 
Assistant Secretary of Defense for Research and Engineering.

Copyright 2017 Massachusetts Institute of Technology.

The software/firmware is provided to you on an As-Is basis

Delivered to the US Government with Unlimited Rights, as defined in DFARS Part 
252.227-7013 or 7014 (Feb 2014). Notwithstanding any copyright notice, U.S. Government 
rights in this work are defined by DFARS 252.227-7013 or DFARS 252.227-7014 as detailed 
above. Use of this work other than as specifically authorized by the U.S. Government may 
violate any copyrights that exist in this work.
'''

import sys
import ima
import tpm_exec
import common    
import select
import time
import hashlib
from keylime import tpm_initialize

def ml_extend(ml,position,searchHash=None):
    f = open(ml,'r')
    
    f.seek(position)
    rest = f.read()
    lines = rest.split('\n')
    runninghash = ima.START_HASH
    
    for line in lines:
        line = line.strip()
        tokens = line.split()
        
        if line =='':
            continue
        if len(tokens)<5:
            print "ERROR: invalid measurement list file line: -%s-"%(line)
            return position
        
        # get the filename roughly
        path = str(line[line.rfind(tokens[3])+len(tokens[3])+1:])        
        template_hash=tokens[1].decode('hex')
        # this is some IMA weirdness
        if template_hash == ima.START_HASH:
            template_hash = ima.FF_HASH
        template_hash = template_hash.encode('hex')
        
        if searchHash is None:
            print "extending hash %s for %s"%(template_hash,path)
            tpm_exec.run("extend -ix %s -ih %s"%(common.IMA_PCR,template_hash))
        else:
            runninghash = hashlib.sha1(runninghash+template_hash.decode('hex')).digest()
            if runninghash.encode('hex') == searchHash:
                position = rest.index(line)+len(line)
                print "Located last IMA file updated: %s"%(path)
                return position
    
    if searchHash is not None:
        raise Exception("Unable to find current measurement list position, Resetting the TPM emulator may be neccesary")
    
    return position+len(rest)

 
def main(argv=sys.argv):
    
    if tpm_initialize.get_tpm_manufacturer() != 'IBM':
        raise Exception("This stub should only be used with the IBM TPM emulator")

    # initialize position in ML
    pos=0

    # check if pcr is clean
    output = tpm_exec.run("pcrread -ix %s"%common.IMA_PCR)[0]
    pcrval = output[0].split()[5]
    
    if pcrval != ima.START_HASH.encode('hex'):
        print "Warning: IMA PCR is not empty, trying to find the last updated file in the measurement list..."
        pos = ml_extend(common.IMA_ML, 0, pcrval)
    
    print "Monitoring %s"%(common.IMA_ML)
    poll_object = select.poll()
    fd_object = file(common.IMA_ML, "r")
    number = fd_object.fileno()
    poll_object.register(fd_object,select.POLLIN|select.POLLPRI)
    
    while True:
        results = poll_object.poll()
        for result in results:
            if result[0] != number:
                continue
            pos = ml_extend(common.IMA_ML,pos)
            #print "new POS %d"%pos
            time.sleep(0.2)
    sys.exit(1)

if __name__ == '__main__':
    main()