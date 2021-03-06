#!/usr/bin/python

'''
DISTRIBUTION STATEMENT A. Approved for public release: distribution unlimited.

This material is based upon work supported by the Assistant Secretary of Defense for 
Research and Engineering under Air Force Contract No. FA8721-05-C-0002 and/or 
FA8702-15-D-0001. Any opinions, findings, conclusions or recommendations expressed in this
material are those of the author(s) and do not necessarily reflect the views of the 
Assistant Secretary of Defense for Research and Engineering.

Copyright 2015 Massachusetts Institute of Technology.

The software/firmware is provided to you on an As-Is basis

Delivered to the US Government with Unlimited Rights, as defined in DFARS Part 
252.227-7013 or 7014 (Feb 2014). Notwithstanding any copyright notice, U.S. Government 
rights in this work are defined by DFARS 252.227-7013 or DFARS 252.227-7014 as detailed 
above. Use of this work other than as specifically authorized by the U.S. Government may 
violate any copyrights that exist in this work.
'''

import string
import os
import tpm_exec
import common
import base64
import tempfile
import traceback
import tpm_random
import secure_mount
import tpm_nvram
import json
import crypto
import sys
from tpm_ek_ca import *
import M2Crypto
from M2Crypto import m2

logger = common.init_logging('tpm_initialize')

global_tpmdata = None

def random_password(length=20):
    rand = crypto.generate_random_key(length)
    chars = string.ascii_uppercase + string.digits + string.ascii_lowercase
    password = ''
    for i in range(length):
        password += chars[ord(rand[i]) % len(chars)]
    return password

def create_ek():
    # this function is intended to be idempotent 
    (output,code,fileout) = tpm_exec.run("createek",raiseOnError=False)
    if code!=tpm_exec.EXIT_SUCESS:
        if len(output)>0 and output[0].startswith("Error Target command disabled from TPM_CreateEndorsementKeyPair"):
            logger.debug("TPM EK already created.")
        elif len(output)>0 and output[0].startswith("Error Defend lock running from TPM_CreateEndorsementKeyPair"):
            logger.debug("createek failed.  TPM locked, will attempt unlock during while taking ownership.  To manually repair run resetlockvalue -pwdo [owner_password]")
        else:
            raise Exception("createek failed with code "+str(code)+": "+str(output))
    return 

def test_ownerpw(owner_pw,reentry=False):
    #make a temp file for the output 
    with tempfile.NamedTemporaryFile() as tmppath:
        (output,code,fileout) = tpm_exec.run("getpubek -pwdo %s -ok %s"%(owner_pw,tmppath.name),raiseOnError=False,outputpath=tmppath.name) 
        if code!=tpm_exec.EXIT_SUCESS:
            if len(output)>0 and output[0].startswith("Error Authentication failed (Incorrect Password) from TPM_OwnerReadPubek"):
                return False
            elif len(output)>0 and output[0].startswith("Error Defend lock running from TPM_OwnerReadPubek"):
                if reentry:
                    logger.error("Unable to unlock TPM")
                    return False
                # tpm got locked. lets try to unlock it
                logger.error("TPM is locked from too many invalid owner password attempts, attempting to unlock with password: %s"%owner_pw)
                # i have no idea why, but runnig this twice seems to actually work
                tpm_exec.run("resetlockvalue -pwdo %s"%owner_pw,raiseOnError=False) 
                tpm_exec.run("resetlockvalue -pwdo %s"%owner_pw,raiseOnError=False) 
                return test_ownerpw(owner_pw,True)
            else:
                raise Exception("test ownerpw, getpubek failed with code "+str(code)+": "+str(output))
    return True

def take_ownership(config_pw):
    owner_pw = get_tpm_metadata("owner_pw")
    ownerpw_known = False
    if not is_tpm_owned():
        # if no ownerpassword
        if config_pw == 'generate':
            logger.info("Generating random TPM owner password")
            owner_pw = random_password(20)
        else:
            logger.info("Taking ownership with config provided TPM owner password: %s"%config_pw)
            owner_pw = config_pw
            
        logger.info("Taking ownership of TPM")
        tpm_exec.run("takeown -pwdo %s -nopubsrk"%owner_pw)
        ownerpw_known = True
    else:
        logger.debug("TPM ownership already taken")
        
    
    # tpm owner_pw still not known, and non provided? bail
    if owner_pw is None and config_pw == 'generate':
        raise Exception("TPM is owned, but owner password has not been provided.  Set config option tpm_ownerpassword to the existing password if known.  If not know, TPM reset is required.")
        
    # now we have owner_pw from tpmdata.json and a config_pw.
    if not ownerpw_known:
        if owner_pw is None or not test_ownerpw(owner_pw):
            logger.info("TPM Owner password: %s from tpmdata.json invalid.  Trying config provided TPM owner password: %s"%(owner_pw,config_pw))
            owner_pw = config_pw
            if not test_ownerpw(owner_pw):
                raise Exception("Config provided owner password %s invalid. Set config option tpm_ownerpassword to the existing password if known.  If not know, TPM reset is required."%owner_pw)                
        ownerpw_known = True
            
    set_tpm_metadata('owner_pw',owner_pw)

    if not ownerpw_known:
        raise Exception("Owner password unknown, TPM reset required")
    else:
        logger.info("TPM Owner password confirmed: %s"%owner_pw)
        
def get_pub_ek(): # assumes that owner_pw is correct at this point
    owner_pw = get_tpm_metadata('owner_pw')
    #make a temp file for the output 
    with tempfile.NamedTemporaryFile() as tmppath:
        (output,code,ek) = tpm_exec.run("getpubek -pwdo %s -ok %s"%(owner_pw,tmppath.name),raiseOnError=False,outputpath=tmppath.name) # generates pubek.pem
        if code!=tpm_exec.EXIT_SUCESS:
            raise Exception("getpubek failed with code "+str(code)+": "+str(output))
    
    set_tpm_metadata('ek',ek)

def create_aik(activate):
    # if no AIK created, then create one
    if get_tpm_metadata('aik') is not None and get_tpm_metadata('aikpriv') is not None and get_tpm_metadata('aikmod') is not None:
        logger.debug("AIK already created")
    else:
        logger.debug("Creating a new AIK identity")
        extra = ""
        if activate:
            extra = "-ac"
        
        owner_pw = get_tpm_metadata('owner_pw')
        aik_pw = random_password(20)
        #make a temp file for the output
        with tempfile.NamedTemporaryFile() as tmppath:
            (retout,code,fileout) = tpm_exec.run("identity -la aik -ok %s -pwdo %s -pwdk %s %s"%(tmppath.name,owner_pw,aik_pw,extra),outputpath=tmppath.name)
            inPem=False
            pem=""
            for line in retout:
                if line.startswith("-----BEGIN PUBLIC KEY-----"):
                    inPem=True
                if inPem:
                    pem+=line
                if line.startswith("-----END PUBLIC KEY-----"):
                    inPem=False
            if pem=="":
                raise Exception("unable to read public aik from create identity.  Is your tpm4720 installation up to date?")
            mod = get_mod_from_pem(pem)
            # read in the output
            if fileout=='':
                raise Exception("unable to read file output.  Is your tpm4720 installation up to date?")
            key = base64.b64encode(fileout)
            
            # persist results
            set_tpm_metadata('aik',pem)
            set_tpm_metadata('aikpriv', key)
            set_tpm_metadata('aikmod',mod)
            set_tpm_metadata('aik_pw',aik_pw)
        if activate:
            logger.debug("Self-activated AIK identity in test mode")
    
    # ensure the AIK is loaded
    handle = load_aik()
    set_tpm_metadata('aik_handle', handle)

def get_mod_from_pem(pem):
    pubkey = crypto.rsa_import_pubkey(pem)
    return base64.b64encode(bytearray.fromhex('{:0192x}'.format(pubkey.n)))

def get_mod_from_tpm(keyhandle):
    (retout,code,fileout)  = tpm_exec.run("getpubkey -ha %s -pwdk %s"%(keyhandle,get_tpm_metadata('aik_pw')),raiseOnError=False)
    if code!=tpm_exec.EXIT_SUCESS and len(retout)>0 and retout[0].startswith("Error Authentication failed (Incorrect Password) from TPM_GetPubKey"):
        return None

    # now to parse things!
    inMod = False
    public_modulus = []
    for line in retout:
        if line.startswith("Modulus"):
            inMod = True
            continue
        if inMod:
            tokens = line.split()
            for token in tokens:
                public_modulus.append(string.atol(token,base=16))
    return base64.b64encode(bytearray(public_modulus))
    

def flush_keys():
    logger.debug("Flushing keys from TPM...") 
    retout = tpm_exec.run("listkeys")[0]
    for line in retout:
        tokens = line.split()
        if len(tokens)==4 and tokens[0]=='Key' and tokens[1]=='handle':
            handle = tokens[3].upper()
            #logger.debug("Flushing key handle %s"%handle)
            tpm_exec.run("flushspecific -ha %s -rt 1"%handle)
            
def load_aik():
    # dont' even try to see if the key is already loaded.  we're flushing them now
#     # is the key already there?
#     modFromFile = get_tpm_metadata('aikmod')
#      
#     retout = tpm_exec.run("listkeys")[0]
#     for line in retout:       
#         tokens = line.split()
#         if len(tokens)==4 and tokens[0]=='Key' and tokens[1]=='handle':
#             handle = tokens[3]
#             modFromTPM = get_mod_from_tpm(handle)
#             if modFromTPM == modFromFile:
#                 logger.debug("Located AIK at key handle %s"%handle)
#                 return handle
#     
#     # we didn't find the key
    logger.debug("Loading AIK private key into TPM")
    
    # write out private key
    with tempfile.NamedTemporaryFile() as inFile:
        inFile.write(base64.b64decode(get_tpm_metadata('aikpriv')))
        inFile.flush()

        retout = tpm_exec.run("loadkey -hp 40000000 -ik %s"%(inFile.name))[0]
        
        if len(retout)>0 and len(retout[0].split())>=4:
            handle = retout[0].split()[4]
        else:
            raise Exception("unable to process output of loadkey %s"%(retout))
    
    return handle.upper()
  
def encryptAIK(uuid,pubaik,pubek):
    pubaikFile=None
    pubekFile=None
    keyblob = None
    blobpath = None
    keypath = None
    
    try:
        # write out pubaik
        pfd, ptemp = tempfile.mkstemp()
        pubaikFile = open(ptemp,"wb")
        pubaikFile.write(pubaik)
        pubaikFile.close()
        os.close(pfd)
        
        # write out the public EK
        efd, etemp = tempfile.mkstemp()
        pubekFile = open(etemp,"wb")
        pubekFile.write(pubek)
        pubekFile.close()
        os.close(efd)
        
        #create temp files for the blob
        blobfd,blobpath = tempfile.mkstemp()
        keyfd,keypath = tempfile.mkstemp()
        
        tpm_exec.run("encaik -ik %s -ek %s -ok %s -oak %s"%(pubaikFile.name,pubekFile.name,blobpath,keypath),lock=False)
        
        logger.info("Encrypting AIK for UUID %s"%uuid)
        
        # read in the blob
        f = open(blobpath,"rb")
        keyblob = base64.b64encode(f.read())
        f.close()
        os.close(blobfd)
        
        # read in the aes key
        f = open(keypath,"rb")
        key = base64.b64encode(f.read())
        f.close()
        os.close(keyfd)
        
    except Exception as e:
        logger.error("Error encrypting AIK: "+str(e))
        logger.error(traceback.format_exc())
        return False
    finally:
        if pubaikFile is not None:
            os.remove(pubaikFile.name)
        if pubekFile is not None:
            os.remove(pubekFile.name)
        if blobpath is not None:
            os.remove(blobpath)
        if keypath is not None:
            os.remove(keypath)
        pass
    return (keyblob,key)

    
def activate_identity(keyblob):
    owner_pw = get_tpm_metadata('owner_pw')
    keyhandle = get_tpm_metadata('aik_handle')
    
    keyblobFile = None
    secpath = None
    try:
        # write out key blob
        kfd, ktemp = tempfile.mkstemp()
        keyblobFile = open(ktemp,"wb")
        keyblobFile.write(base64.b64decode(keyblob))
        keyblobFile.close()
        os.close(kfd)
        
        # ok lets write out the key now
        secdir=secure_mount.mount() # confirm that storage is still securely mounted
            
        secfd,secpath=tempfile.mkstemp(dir=secdir)
        
        command = "activateidentity -hk %s -pwdo %s -pwdk %s -if %s -ok %s"%(keyhandle,owner_pw,get_tpm_metadata('aik_pw'),keyblobFile.name,secpath)
        (retout,code,fileout) = tpm_exec.run(command,outputpath=secpath)
        logger.info("AIK activated.")
        
        key = base64.b64encode(fileout)
        os.close(secfd)
        os.remove(secpath)
        
    except Exception as e:
        logger.error("Error decrypting AIK: "+str(e))
        logger.error(traceback.format_exc())
        return False
    finally:
        if keyblobFile is not None:
            os.remove(keyblobFile.name)
        if secpath is not None and os.path.exists(secpath):
            os.remove(secpath)
    return key

#openssl x509 -inform der -in certificate.cer -out certificate.pem
def verify_ek(ekcert,ekpem):
    """Verify that the provided EK certificate is signed by a trusted root
    :param ekcert: The Endorsement Key certificate in DER format
    :param ekpem: the endorsement public key in PEM format
    :returns: True if the certificate can be verified, false otherwise
    """
    try:
        pubekmod = base64.b64decode(get_mod_from_pem(ekpem))
        
        ek509 = M2Crypto.X509.load_cert_der_string(ekcert)
        
        # locate the region where the pub ek should be and then brute force looking for it.  this is awful!
        # Sadly TPM ek certificates are corrupted in a way that openssl and most other utilities can't read them.
        # sigh TCG
        #
        # search for first 1.2.840.113549.1.1.5 (OID for sha1WithRSAEncryption (PKCS #1))
        start = ekcert.index('2a864886f70d010107'.decode('hex'))
        # now locate the next 1.2.840.113549.1.1.7 (OID for rsaOAEP (PKCS #1)) afterwards
        end = ekcert.index('2a864886f70d010105'.decode('hex'),start)
        
        if str(pubekmod) not in str(ekcert[start:end]):
            logger.error("Public EK does not match EK certificate")
            return False
        
        for signer in trusted_certs:
            signcert = M2Crypto.X509.load_cert_string(trusted_certs[signer])
            signkey = signcert.get_pubkey()
            if ek509.verify(signkey) == 1:
                logger.debug("EK cert matched signer %s"%signer)
                return True

        for key in atmel_trusted_keys:
            e = m2.bn_to_mpi(m2.hex_to_bn(atmel_trusted_keys[key]['exponent']))
            n = m2.bn_to_mpi(m2.hex_to_bn(atmel_trusted_keys[key]['key']))
            rsa = M2Crypto.RSA.new_pub_key((e, n))
            pubkey = M2Crypto.EVP.PKey()
            pubkey.assign_rsa(rsa)
            
            if ek509.verify(pubkey) == 1:
                logger.debug("EK cert matched trusted key %s"%key)
                return True
    except Exception as e:
        # Log the exception so we don't lose the raw message 
        logger.exception(e)
        raise Exception("Error processing ek/ekcert. Does this TPM have a valid EK?"), None, sys.exc_info()[2]
    
    logger.error("No Root CA matched EK Certificate")
    return False

def get_tpm_manufacturer():
    retout = tpm_exec.run("getcapability -cap 1a")[0]
    for line in retout:
        tokens = line.split()
        if len(tokens)== 3 and tokens[0]=='VendorID' and tokens[1]==':':
            #logger.debug("TPM vendor id: %s",tokens[2])
            return tokens[2]
    return None

def is_emulator():
    return 'IBM'==get_tpm_manufacturer()

def is_vtpm():
    if common.STUB_VTPM:
        return True
    else:
        return 'ETHZ'==get_tpm_manufacturer()

def is_tpm_owned():
    retout = tpm_exec.run("getcapability -cap 5 -scap 111")[0]
    tokens = retout[0].split()
    if tokens[-1]=='TRUE':
        return True
    else:
        return False

def read_tpm_data():
    if os.path.exists('tpmdata.json'):
        with open('tpmdata.json','r') as f:
            return json.load(f)
    else:
        return {}
    
def write_tpm_data():
    global global_tpmdata
    os.umask(0o077)
    if os.geteuid()!=0 and common.REQUIRE_ROOT:
        logger.warning("Creating tpm metadata file without root.  Sensitive trust roots may be at risk!")
    with open('tpmdata.json','w') as f:
        json.dump(global_tpmdata,f)

def get_tpm_metadata(key):
    global global_tpmdata
    if global_tpmdata == None:
        global_tpmdata = read_tpm_data()
    return global_tpmdata.get(key,None)

def set_tpm_metadata(key,value):
    global global_tpmdata
    if global_tpmdata == None:
        global_tpmdata = read_tpm_data()
    
    if global_tpmdata.get(key,None) is not value:
        global_tpmdata[key]=value
        write_tpm_data()

def init(self_activate=False,config_pw=None):
    create_ek()
    take_ownership(config_pw)
    
    get_pub_ek()

    ekcert = tpm_nvram.read_ekcert_nvram()
    set_tpm_metadata('ekcert',ekcert)
    
    # if no AIK created, then create one
    create_aik(self_activate)
    
    return get_tpm_metadata('ek'),get_tpm_metadata('ekcert'),get_tpm_metadata('aik')
    
if __name__=="__main__":
    try:
        init(True,'test')
    except Exception as e:
        logger.exception(e)
