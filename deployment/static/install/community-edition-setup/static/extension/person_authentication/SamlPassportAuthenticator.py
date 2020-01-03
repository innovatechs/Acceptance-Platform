# oxAuth is available under the MIT License (2008). See http://opensource.org/licenses/MIT for full text.
# Copyright (c) 2019, Gluu
#
# Author: Jose Gonzalez
# Author: Yuriy Movchan
#
from org.gluu.jsf2.service import FacesService
from org.gluu.jsf2.message import FacesMessages

from org.gluu.oxauth.model.common import User, WebKeyStorage
from org.gluu.oxauth.model.configuration import AppConfiguration
from org.gluu.oxauth.model.crypto import CryptoProviderFactory
from org.gluu.oxauth.model.jwt import Jwt, JwtClaimName
from org.gluu.oxauth.model.util import Base64Util
from org.gluu.oxauth.service import AppInitializer, AuthenticationService, UserService
from org.gluu.oxauth.model.authorize import AuthorizeRequestParam
from org.gluu.oxauth.service.net import HttpService
from org.gluu.oxauth.security import Identity
from org.gluu.oxauth.util import ServerUtil
from org.gluu.config.oxtrust import LdapOxPassportConfiguration
from org.gluu.model.custom.script.type.auth import PersonAuthenticationType
from org.gluu.service.cdi.util import CdiUtil
from org.gluu.util import StringHelper
from java.util import ArrayList, Arrays, Collections, HashSet

from javax.faces.application import FacesMessage
from javax.faces.context import FacesContext

import json
import sys
import uuid

class PersonAuthentication(PersonAuthenticationType):
    def __init__(self, currentTimeMillis):
        self.currentTimeMillis = currentTimeMillis

    def init(self, configurationAttributes):
        print "Passport. init called"

        self.extensionModule = self.loadExternalModule(configurationAttributes.get("extension_module"))
        extensionResult = self.extensionInit(configurationAttributes)
        if extensionResult != None:
            return extensionResult

        print "Passport. init. Behaviour is inbound SAML"
        success = self.processKeyStoreProperties(configurationAttributes)

        #COLLECT - Parse the list of RPs and IDP that we need to collect for
        success = self.processMappingCollectionFilters(configurationAttributes)

        if success:
            self.providerKey = "provider"
            self.customAuthzParameter = self.getCustomAuthzParameter(configurationAttributes.get("authz_req_param_provider"))
            self.passportDN = self.getPassportConfigDN()
            print "Passport. init. Initialization success"
        else:
            print "Passport. init. Initialization failed"
        return success


    def destroy(self, configurationAttributes):
        print "Passport. destroy called"
        return True


    def getApiVersion(self):
        return 2


    def isValidAuthenticationMethod(self, usageType, configurationAttributes):
        print "Passport. isValidAuthenticationMethod called"
        
        sessionAttributes = CdiUtil.bean(Identity).getSessionId().getSessionAttributes()
        authenticatedSourceProvider = sessionAttributes.get("authenticatedProvider")
        # COLLECT - we do not want to interrupt collection if in progress
        collectSamlPass = sessionAttributes.get("collectSamlPass")
        if ( collectSamlPass != 1 ):
            # SWITCH - invalidate this authentication only if the switchFlow is ON
            if ( sessionAttributes.get("switchFlowStatus") == "1_GET_SOURCE" and sessionAttributes.get("switchSourceAuthenticatedProvider") != None ):
                print "Passport DEBUG. isValidAuthenticationMethod SWITCH FLOW set to 1_GET_SOURCE, auth complete, returning False"
                return False
            elif ( sessionAttributes.get("switchFlowStatus") == "2_GET_TARGET" and sessionAttributes.get("switchTargetAuthenticatedProvider") != None ):
                print "Passport DEBUG. isValidAuthenticationMethod SWITCH FLOW set to 2_GET_TARGET, auth complete, returning False"
                return False
        if ( sessionAttributes.get("mfaFlowStatus") == "MFA_2_IN_PROGRESS" ):
            print "Passport DEBUG. isValidAuthenticationMethod MFA FLOW set to MFA_2_IN_PROGRESS, auth complete, returning False"
            return False
        return True


    def getAlternativeAuthenticationMethod(self, usageType, configurationAttributes):
        print "Passport. getAlternativeAuthenticationMethod called"
        mfaFlowStatus = CdiUtil.bean(Identity).getSessionId().getSessionAttributes().get("mfaFlowStatus")
        if ( mfaFlowStatus == "MFA_2_IN_PROGRESS" ):
            return "passport_social"
        return "select_loa2"


    def authenticate(self, configurationAttributes, requestParameters, step):

        extensionResult = self.extensionAuthenticate(configurationAttributes, requestParameters, step)
        if extensionResult != None:
            return extensionResult

        print "Passport. authenticate for step %s called" % str(step)
        identity = CdiUtil.bean(Identity)

        if step == 1:
            jwt_param = None
            if self.isInboundFlow(identity):
                print "Passport. authenticate for step 1. Detected idp-initiated inbound Saml flow"
                jwt_param = identity.getSessionId().getSessionAttributes().get(AuthorizeRequestParam.STATE)

            if jwt_param == None:
                jwt_param = ServerUtil.getFirstValue(requestParameters, "user")

            if jwt_param != None:
                print "Passport. authenticate for step 1. JWT user profile token found"

                # Parse JWT and validate
                jwt = Jwt.parse(jwt_param)
                if not self.validSignature(jwt):
                    return False

                if self.jwtHasExpired(jwt):
                    return False

                (user_profile, jsonp) = self.getUserProfile(jwt)
                if user_profile == None:
                    print "Passport. authenticate for step 1. [user_profile] is not found in response!"
                    return False

                return self.attemptAuthentication(identity, user_profile, jsonp)

            #See passportlogin.xhtml
            provider = ServerUtil.getFirstValue(requestParameters, "loginForm:provider")
            if StringHelper.isEmpty(provider):

                #it's username + passw auth
                print "Passport. authenticate for step 1. Basic authentication detected"
                logged_in = False

                credentials = identity.getCredentials()
                user_name = credentials.getUsername()
                user_password = credentials.getPassword()

                if StringHelper.isNotEmptyString(user_name) and StringHelper.isNotEmptyString(user_password):
                    authenticationService = CdiUtil.bean(AuthenticationService)
                    logged_in = authenticationService.authenticate(user_name, user_password)

                print "Passport. authenticate for step 1. Basic authentication returned: %s" % logged_in
                return logged_in

            elif provider in self.registeredProviders:
                #it's a recognized external IDP
                identity.setWorkingParameter("selectedProvider", provider)
                print "Passport. authenticate for step 1. Retrying step 1"
                #see prepareForStep (step = 1)
                return True

        if step == 2:
            mail = ServerUtil.getFirstValue(requestParameters, "loginForm:email")
            jsonp = identity.getWorkingParameter("passport_user_profile")

            if mail == None:
                self.setMessageError(FacesMessage.SEVERITY_ERROR, "Email was missing in user profile")
            elif jsonp != None:
                # Completion of profile takes place
                user_profile = json.loads(jsonp)
                user_profile["mail"] = mail

                return self.attemptAuthentication(identity, user_profile, jsonp)

            print "Passport. authenticate for step 2. Failed: expected mail value in HTTP request and json profile in session"
            return False


    def prepareForStep(self, configurationAttributes, requestParameters, step):

        extensionResult = self.extensionPrepareForStep(configurationAttributes, requestParameters, step)
        if extensionResult != None:
            return extensionResult

        print "Passport. prepareForStep called for step %s"  % str(step)
        identity = CdiUtil.bean(Identity)

        if step == 1:
            #re-read the strategies config (for instance to know which strategies have enabled the email account linking)
            self.parseProviderConfigs()
            identity.setWorkingParameter("externalProviders", json.dumps(self.registeredProviders))

            providerParam = self.customAuthzParameter
            url = None

            print "Passport. prepareForStep. got session '%s'"  % identity.getSessionId().toString()
            
            sessionAttributes = identity.getSessionId().getSessionAttributes()
            self.skipProfileUpdate = StringHelper.equalsIgnoreCase(sessionAttributes.get("skipPassportProfileUpdate"), "true")

            # This is added to the script by a previous module if the provider is preselected
            providerFromSession = sessionAttributes.get("selectedProvider")
            if providerFromSession != None:
                print "Passport. prepareForStep. Setting selectedProvider from session  = '%s'" % providerFromSession
                identity.setWorkingParameter("selectedProvider", providerFromSession)            
                # SWITCH - Reset the provider in session in case the choice has to be made again
                sessionAttributes.remove("selectedProvider")
                sessionAttributes.remove("new_acr_value")

            issuerSpNameQualifier = sessionAttributes.get("spNameQualifier")
            
            if issuerSpNameQualifier != None:
                # Reset the issuer in session in case the choice has to be made again
                print "Passport. prepareForStep. Setting SAML SP (issuer spNameQualifier) = '%s'" % issuerSpNameQualifier
                
            # get the switch state
            switchFlowStatus = sessionAttributes.get("switchFlowStatus")
            
            # COLLECT - make sure we collect only if provider and spNameQualifier (RP) are both on the list
            collectSamlPass = sessionAttributes.get("collectSamlPass")
            if ( switchFlowStatus != "2_GET_TARGET" and providerFromSession in self.idp_to_collect_old_mappings_from and issuerSpNameQualifier in self.rps_to_collect_old_mappings_for):
                # mark collection pass 1 or 2 depending on the session variable
                if (collectSamlPass == None):
                    collectSamlPass = 1
                elif (collectSamlPass == 1):
                    collectSamlPass = 2
                # save the collection pass marker
                print "Passport. prepareForStep. COLLECTING - Setting collection pass to '%s'" % collectSamlPass
                sessionAttributes.put("collectSamlPass", collectSamlPass)
            else:
                sessionAttributes.remove("collectSamlPass")

            #this param could have been set previously in authenticate step if current step is being retried
            provider = identity.getWorkingParameter("selectedProvider")
            if provider != None:
                # during the first pass or the non-collecting flow we ask with the default SIC issuer only
                if (collectSamlPass == None):
                    # Terimante the flow by removing the selected provider
                    identity.setWorkingParameter("selectedProvider", None)
                    # We are not collecting, just submit the normal default issuer
                    print "Passport. prepareForStep. NOT COLLECTING, getting the URL for passport"
                    url = self.getPassportRedirectUrl(provider, None)

                elif (collectSamlPass == 1):
                    # First time around collecting, we login as provider without specifying providerIssuer
                    print "Passport. prepareForStep. COLLECTING - First pass, setting URL to use issuerSpNameQualifier = None"
                    url = self.getPassportRedirectUrl(provider, None)

                elif (collectSamlPass == 2):
                    # SWITCH - Resetting selected provider as its purpose has been used up and in case of switching it needs to be re-selected
                    identity.setWorkingParameter("selectedProvider", None)
                    # During the second pass we check the user identity for 
                    print "Passport. prepareForStep. COLLECTING - Second pass, setting URL to use issuerSpNameQualifier = '%s'" % issuerSpNameQualifier
                    url = self.getPassportRedirectUrl(provider, issuerSpNameQualifier)

            elif providerParam != None:
                paramValue = sessionAttributes.get(providerParam)

                if paramValue != None:
                    print "Passport. prepareForStep. Found value in custom param of authorization request: %s" % paramValue
                    provider = self.getProviderFromJson(paramValue)

                    if provider == None:
                        print "Passport. prepareForStep. A provider value could not be extracted from custom authorization request parameter"
                    elif not provider in self.registeredProviders:
                        print "Passport. prepareForStep. Provider '%s' not part of known configured IDPs/OPs" % provider
                    else:
                        url = self.getPassportRedirectUrl(provider, issuerSpNameQualifier)

            if url == None:
                print "Passport. prepareForStep. A page to manually select an identity provider will be shown"
            else:
                facesService = CdiUtil.bean(FacesService)
                facesService.redirectToExternalURL(url)

        return True


    def getExtraParametersForStep(self, configurationAttributes, step):
        print "Passport. getExtraParametersForStep called with step %s" % str(step)
        if step == 1:
            return Arrays.asList("selectedProvider", "selectedProviderIssuer", "externalProviders")
        elif step == 2:
            return Arrays.asList("passport_user_profile")
        return None


    def getCountAuthenticationSteps(self, configurationAttributes):
        print "Passport. getCountAuthenticationSteps called"
        identity = CdiUtil.bean(Identity)
        if identity.getWorkingParameter("passport_user_profile") != None:
            return 2
        if identity.getSessionId().getSessionAttributes().get("switchFlowStatus") != None:
            return 2
        if identity.getSessionId().getSessionAttributes().get("mfaFlowStatus") != None:
            return 2
        return 1


    def getPageForStep(self, configurationAttributes, step):
        print "Passport. getPageForStep called with step %s" % str(step)

        extensionResult = self.extensionGetPageForStep(configurationAttributes, step)
        if extensionResult != None:
            return extensionResult

        if step == 1:
            identity = CdiUtil.bean(Identity)
            if self.isInboundFlow(identity):
                print "Passport. getPageForStep for step 1. Detected inbound Saml flow"
                return "/postlogin.xhtml"

            return "/auth/passport/passportlogin.xhtml"

        return "/auth/passport/passportpostlogin.xhtml"


    def getNextStep(self, configurationAttributes, requestParameters, step):
        if step == 1:
            identity = CdiUtil.bean(Identity)
            provider = identity.getWorkingParameter("selectedProvider")
            if provider != None:
                print "Passport DEBUG getNextStep. returning 1"
                return 1

        print "Passport DEBUG getNextStep. returning -1"
        return -1


    def logout(self, configurationAttributes, requestParameters):
        return True

# Extension module related functions

    def extensionInit(self, configurationAttributes):

        if self.extensionModule == None:
            return None
        return self.extensionModule.init(configurationAttributes)


    def extensionAuthenticate(self, configurationAttributes, requestParameters, step):

        if self.extensionModule == None:
            return None
        return self.extensionModule.authenticate(configurationAttributes, requestParameters, step)


    def extensionPrepareForStep(self, configurationAttributes, requestParameters, step):

        if self.extensionModule == None:
            return None
        return self.extensionModule.prepareForStep(configurationAttributes, requestParameters, step)


    def extensionGetPageForStep(self, configurationAttributes, step):

        if self.extensionModule == None:
            return None
        return self.extensionModule.getPageForStep(configurationAttributes, step)

# Initalization routines

    def loadExternalModule(self, simpleCustProperty):

        if simpleCustProperty != None:
            print "Passport. loadExternalModule. Loading passport extension module..."
            moduleName = simpleCustProperty.getValue2()
            try:
                module = __import__(moduleName)
                return module
            except:
                print "Passport. loadExternalModule. Failed to load module %s" % moduleName
                print "Exception: ", sys.exc_info()[1]
                print "Passport. loadExternalModule. Flow will be driven entirely by routines of main passport script"
        return None


    def processKeyStoreProperties(self, attrs):
        file = attrs.get("key_store_file")
        password = attrs.get("key_store_password")

        if file != None and password != None:
            file = file.getValue2()
            password = password.getValue2()

            if StringHelper.isNotEmpty(file) and StringHelper.isNotEmpty(password):
                self.keyStoreFile = file
                self.keyStorePassword = password
                return True

        print "Passport. readKeyStoreProperties. Properties key_store_file or key_store_password not found or empty"
        return False


    def processMappingCollectionFilters(self, attrs):
        param_idp = attrs.get("idp_to_collect_old_mappings_from")
        param_rps = attrs.get("rps_to_collect_old_mappings_for")

        self.idp_to_collect_old_mappings_from = []
        self.rps_to_collect_old_mappings_for  = []

        # COLLECT - Parse the list of RPs and IDP that we need to collect for
        if param_idp != None and param_rps != None:
            idpList = param_idp.getValue2()
            rpList = param_rps.getValue2()

            if StringHelper.isNotEmpty(idpList) and StringHelper.isNotEmpty(rpList):
                self.idp_to_collect_old_mappings_from = StringHelper.split(idpList,',')
                self.rps_to_collect_old_mappings_for  = StringHelper.split(rpList,',')    
                print "Passport. init. COLLECTING mappings for IDPs [ %s ]" % ', '.join(self.idp_to_collect_old_mappings_from)
                print "Passport. init. COLLECTING mappings for RPs [ %s ]" % ', '.join(self.rps_to_collect_old_mappings_for)
                return True

        print "Passport. init. NOT COLLECTING any mappings, parameters [idp_to_collect_old_mappings_from] and [rps_to_collect_old_mappings_for] missing/empty."
        return True

    def getCustomAuthzParameter(self, simpleCustProperty):

        customAuthzParameter = None
        if simpleCustProperty != None:
            prop = simpleCustProperty.getValue2()
            if StringHelper.isNotEmpty(prop):
                customAuthzParameter = prop

        if customAuthzParameter == None:
            print "Passport. getCustomAuthzParameter. No custom param for OIDC authz request in script properties"
            print "Passport. getCustomAuthzParameter. Passport flow cannot be initiated by doing an OpenID connect authorization request"
        else:
            print "Passport. getCustomAuthzParameter. Custom param for OIDC authz request in script properties: %s" % customAuthzParameter

        return customAuthzParameter

# Configuration parsing

    def getPassportConfigDN(self):

        f = open('/etc/gluu/conf/gluu.properties', 'r')
        for line in f:
            prop = line.split("=")
            if prop[0] == "oxpassport_ConfigurationEntryDN":
              prop.pop(0)
              break

        f.close()
        return "=".join(prop).strip()


    def parseAllProviders(self):

        registeredProviders = {}
        print "Passport. parseAllProviders. Adding providers"
        entryManager = CdiUtil.bean(AppInitializer).createPersistenceEntryManager()

        config = LdapOxPassportConfiguration()
        config = entryManager.find(config.getClass(), self.passportDN).getPassportConfiguration()
        config = config.getProviders() if config != None else config

        if config != None and len(config) > 0:
            for prvdetails in config:
                if prvdetails.isEnabled():
                    registeredProviders[prvdetails.getId()] = {
                        "emailLinkingSafe": prvdetails.isEmailLinkingSafe(),
                        "requestForEmail" : prvdetails.isRequestForEmail(),
                        "logo_img": prvdetails.getLogoImg(),
                        "displayName": prvdetails.getDisplayName(),
                        "type": prvdetails.getType(),
                        "issuer": prvdetails.getOptions().get("issuer")
                    }

        return registeredProviders


    def parseProviderConfigs(self):

        registeredProviders = {}
        try:
            registeredProviders = self.parseAllProviders()
            toRemove = []

            for provider in registeredProviders:
                if registeredProviders[provider]["type"] != "saml":
                    toRemove.append(provider)
                else:
                    registeredProviders[provider]["saml"] = True

            for provider in toRemove:
                registeredProviders.pop(provider)

            if len(registeredProviders.keys()) > 0:
                print "Passport. parseProviderConfigs. Configured providers:", registeredProviders
            else:
                print "Passport. parseProviderConfigs. No providers registered yet"
        except:
            print "Passport. parseProviderConfigs. An error occurred while building the list of supported authentication providers", sys.exc_info()[1]

        self.registeredProviders = registeredProviders

# Auxiliary routines

    def getProviderFromJson(self, providerJson):

        provider = None
        try:
            obj = json.loads(Base64Util.base64urldecodeToString(providerJson))
            provider = obj[self.providerKey]
        except:
            print "Passport. getProviderFromJson. Could not parse provided Json string. Returning None"

        return provider


    def getPassportRedirectUrl(self, provider, issuerSpNameQualifier):

        # provider is assumed to exist in self.registeredProviders
        url = None
        try:
            facesContext = CdiUtil.bean(FacesContext)
            tokenEndpoint = "https://%s/passport/token" % facesContext.getExternalContext().getRequest().getServerName()

            httpService = CdiUtil.bean(HttpService)
            httpclient = httpService.getHttpsClient()

            print "Passport. getPassportRedirectUrl. Obtaining token from passport at %s" % tokenEndpoint
            resultResponse = httpService.executeGet(httpclient, tokenEndpoint, Collections.singletonMap("Accept", "text/json"))
            httpResponse = resultResponse.getHttpResponse()
            bytes = httpService.getResponseContent(httpResponse)

            response = httpService.convertEntityToString(bytes)
            print "Passport. getPassportRedirectUrl. Response was %s" % httpResponse.getStatusLine().getStatusCode()

            if (issuerSpNameQualifier == None):
                issuerSpNameQualifier = "0"

            print "Passport. getPassportRedirectUrl. Loading response %s" % response
            tokenObj = json.loads(response)
            print "Passport. getPassportRedirectUrl. Building URL: provider:  %s" % provider
            print "Passport. getPassportRedirectUrl. Building URL: token:     %s" % tokenObj["token_"]
            print "Passport. getPassportRedirectUrl. Building URL: spNameQfr: %s" % issuerSpNameQualifier
            url = "/passport/auth/%s/%s/saml/%s" % (provider, tokenObj["token_"], Base64Util.base64urlencode(issuerSpNameQualifier))
        except:
            print "Passport. getPassportRedirectUrl. Error building redirect URL: ", sys.exc_info()[1]

        return url


    def validSignature(self, jwt):

        print "Passport. validSignature. Checking JWT token signature"
        valid = False

        try:
            appConfiguration = AppConfiguration()
            appConfiguration.setWebKeysStorage(WebKeyStorage.KEYSTORE)
            appConfiguration.setKeyStoreFile(self.keyStoreFile)
            appConfiguration.setKeyStoreSecret(self.keyStorePassword)

            cryptoProvider = CryptoProviderFactory.getCryptoProvider(appConfiguration)
            valid = cryptoProvider.verifySignature(jwt.getSigningInput(), jwt.getEncodedSignature(), jwt.getHeader().getKeyId(),
                                                        None, None, jwt.getHeader().getAlgorithm())
        except:
            print "Exception: ", sys.exc_info()[1]

        print "Passport. validSignature. Validation result was %s" % valid
        return valid

    def jwtHasExpired(self, jwt):
        # Check if jwt has expired
        jwt_claims = jwt.getClaims()
        try:
            exp_date = jwt_claims.getClaimAsDate(JwtClaimName.EXPIRATION_TIME)
            hasExpired = exp_date < datetime.now()
        except:
            print "Exception: The JWT does not have '%s' attribute" % JwtClaimName.EXPIRATION_TIME
            return False

        return hasExpired

    def getUserProfile(self, jwt):
        # Check if there is user profile
        jwt_claims = jwt.getClaims()
        user_profile_json = jwt_claims.getClaimAsString("data")
        if StringHelper.isEmpty(user_profile_json):
            print "Passport. getUserProfile. User profile missing in JWT token"
            user_profile = None
        else:
            user_profile = json.loads(user_profile_json)

        return (user_profile, user_profile_json)


    def attemptAuthentication(self, identity, user_profile, user_profile_json):

        uidKey = "uid"
        
        print "Passport. attemptAuthentication. got session '%s'"  % identity.getSessionId().toString()
        sessionAttributes = identity.getSessionId().getSessionAttributes()
        collectSamlPass = sessionAttributes.get("collectSamlPass")
        switchFlowStatus = sessionAttributes.get("switchFlowStatus")

        if (collectSamlPass != 2):
            # COLLECT - Do not check user attributes if user has already been authenticated
            if not self.checkRequiredAttributes(user_profile, [uidKey, self.providerKey]):
                return False

        provider = user_profile[self.providerKey]
        if not provider in self.registeredProviders:
            print "Passport. attemptAuthentication. Identity Provider %s not recognized" % provider
            return False

        # We assign the UID from the response as the SAML uid by default
        uid = user_profile[uidKey][0]

        # PERSISTENT_ID - save the original one generated by passport for collection purposes in second pass
        passportPersistentId = user_profile["persistentId"][0]
        # PERSISTENT_ID - generate the persistentId for the RP in case there is no further processing/collection happening (SAML only)
        newPersistentId = None
        newPersistentIdRp = sessionAttributes.get("spNameQualifier")
        if ( newPersistentIdRp != None ):
            newPersistentIdIdp = self.registeredProviders[provider]["issuer"]
            newPersistentIdUid = "sic" + uuid.uuid4().hex
            newPersistentId = '%s|%s|%s' % (newPersistentIdRp, newPersistentIdIdp, newPersistentIdUid )

        # SWITCH - do NOT generate a new persistentId if the switch flow is being executed and not collecting
        if  ( switchFlowStatus == None or collectSamlPass != None ):
            user_profile["persistentId"][0] = newPersistentId
        else:
            user_profile.pop("persistentId");

        # COLLECT - In this block we manipulate the "uid" and "persistentId" according to login or capturing pass
        if (collectSamlPass == 1):

            # The first time around we save the UID in the session parameter
            print "Passport. attemptAuthentication. COLLECTING - First Pass. Saving original UID in session as '%s'"  % uid
            sessionAttributes.put("collect_originalUid", uid)

            # Removing persistentId from initial save because we need to run collection first
            print "Passport. attemptAuthentication. COLLECTING - First Pass. Saving generated PersistentId for second pass to '%s'"  % user_profile["persistentId"][0]
            sessionAttributes.put("collect_generatedPersistentId", user_profile["persistentId"][0])
            user_profile.pop("persistentId")

        elif (collectSamlPass == 2):
            # The second time around we retrieve the saved UID
            print "Passport. attemptAuthentication. COLLECTING - Second Pass. Authenticated for collection as '%s'"  % uid

            # Here we verify if there was no answer (GCKey) because allowCreate=false
            if (user_profile == None):
                print "Passport. attemptAuthentication. Aw Crap, user_profile in response is None for original UID '%s'"  % uid
            elif (user_profile[uidKey] == None):
                print "Passport. attemptAuthentication. Aw Crap, user_profile[uidKey] in response is None for original UID '%s'"  % uid
                #user_profile[persistentId] = []
                #user_profile[persistentId].append(generatedPersistentId)
            elif (user_profile[uidKey][0] == None):
                print "Passport. attemptAuthentication. Aw Crap, user_profile[uidKey][0] in response is None for original UID '%s'"  % uid
            else:
                # COLLECT - Collect the persistent ID / PAI for the RP here
                # 1. take old persistentId and split by |
                # 2. replace the RP and keep provider and the PAI UID
                # 3. lastly put it back into the profile mapping and put the original UID back into the profile
                print "Passport. attemptAuthentication. COLLECTING - Second Pass. Original persistentId from passport '%s'"  %  user_profile["persistentId"][0]
                rpPersistentId = passportPersistentId.split('|')
                newPersistentIdIdp = rpPersistentId[1]
                newPersistentIdUid = rpPersistentId[2]
                user_profile["persistentId"][0] = '%s|%s|%s' % (newPersistentIdRp, newPersistentIdIdp, newPersistentIdUid )
                print "Passport. attemptAuthentication. COLLECTING - Second Pass. Collected persistentId '%s'"  % user_profile["persistentId"][0]

                uid = sessionAttributes.get("collect_originalUid")
                user_profile[uidKey][0] = uid
                print "Passport. attemptAuthentication. COLLECTING - Second Pass. Setting profile to original UID '%s'"  % uid

            sessionAttributes.remove("collectSamlPass")                
            sessionAttributes.remove("collect_originalUid")
            sessionAttributes.remove("collect_generatedPersistentId")

        externalUid = "passport-%s:%s:%s" % ("saml", provider, uid)

        print "Passport. attemptAuthentication. Searching for user ExternalUID '%s'" % externalUid

        userService = CdiUtil.bean(UserService)
        userByUid = self.getUserByExternalUid(uid, provider, userService)

        # COLLECT - We will never use email in our data
        email = None
        if "mail" in user_profile:
            email = user_profile["mail"]
            if len(email) == 0:
                email = None
            else:
                email = email[0]
                user_profile["mail"] = [ email ]

        if email == None and self.registeredProviders[provider]["requestForEmail"]:
            print "Passport. attemptAuthentication. Email was not received"

            if userByUid != None:
                # COLLECT - if collecting we check for existing persistentIds for RP to skip second call
                if (collectSamlPass == 1):
                    userPersistentIds = userByUid.getAttributeValues("persistentId")
                    if ( newPersistentIdRp != None and userPersistentIds != None ):
                        if ( userPersistentIds.size > 0 ):
                            # go through existing user persistentIds
                            for userPersistentId in userPersistentIds:
                                existingMappedRp = StringHelper.split(userPersistentId,'|')[0]
                                # if the current RP already has a mapping then skip the second phase
                                if ( userPersistentId.find(newPersistentIdRp) > -1 ):
                                    sessionAttributes.remove("selectedProvider")

                # This avoids asking for the email over every login attempt
                email = userByUid.getAttribute("mail")
                if email != None:
                    print "Passport. attemptAuthentication. Filling missing email value with %s" % email
                    user_profile["mail"] = [ email ]

            if email == None:
                # Store user profile in session and abort this routine
                identity.setWorkingParameter("passport_user_profile", user_profile_json)
                return True

        # COLLECT - we will never store email addresses or match via EMAIL, skip to speed up processing
        # userByMail = None if email == None else userService.getUserByAttribute("mail", email)
        userByMail = None

        # Determine if we should add entry, update existing, or deny access
        doUpdate = False
        doAdd = False
        if userByUid != None:
            print "User with externalUid '%s' already exists" % externalUid
            if userByMail == None:
                doUpdate = True
            else:
                if userByMail.getUserId() == userByUid.getUserId():
                    doUpdate = True
                else:
                    print "Users with externalUid '%s' and mail '%s' are different. Access will be denied. Impersonation attempt?" % (externalUid, email)
                    self.setMessageError(FacesMessage.SEVERITY_ERROR, "Email value corresponds to an already existing provisioned account")
        else:
            if userByMail == None:
                doAdd = True
            elif self.registeredProviders[provider]["emailLinkingSafe"]:

                tmpList = userByMail.getAttributeValues("oxExternalUid")
                tmpList = ArrayList() if tmpList == None else ArrayList(tmpList)
                tmpList.add(externalUid)
                userByMail.setAttribute("oxExternalUid", tmpList)

                userByUid = userByMail
                print "External user supplying mail %s will be linked to existing account '%s'" % (email, userByMail.getUserId())
                doUpdate = True
            else:
                print "An attempt to supply an email of an existing user was made. Turn on 'emailLinkingSafe' if you want to enable linking"
                self.setMessageError(FacesMessage.SEVERITY_ERROR, "Email value corresponds to an already existing account. If you already have a username and password use those instead of an external authentication site to get access.")

        username = None
        try:
            if doUpdate:
                username = userByUid.getUserId()
                user_profile[uidKey][0] = username
                print "Passport. attemptAuthentication. Updating user %s" % username
                self.updateUser(userByUid, user_profile, userService)
            elif doAdd:
                print "Passport. attemptAuthentication. Creating user %s" % externalUid
                user_profile[uidKey][0] = uuid.uuid4().hex
                newUser = self.addUser(externalUid, user_profile, userService)
                username = newUser.getUserId()
        except:
            print "Exception: ", sys.exc_info()[1]
            print "Passport. attemptAuthentication. Authentication failed"
            return False

        if username == None:
            print "Passport. attemptAuthentication. Authentication attempt was rejected"
            return False
        else:
            logged_in = CdiUtil.bean(AuthenticationService).authenticate(username)
            print "Passport. attemptAuthentication. Authentication for %s returned %s" % (username, logged_in)
            if ( logged_in == True ):
                # Save the authenticated data 
                sessionAttributes.put("authenticatedProvider", "passport_saml:" + provider)
                sessionAttributes.put("authenticatedUser", username)
                # SWITCH - Save contextual data for the switch flows
                if (switchFlowStatus == "1_GET_SOURCE"):
                    print "Passport. attemptAuthentication. SWITCH FLOW: Setting SOURCE provider to %s" % sessionAttributes.get("authenticatedProvider")
                    sessionAttributes.put( "switchSourceAuthenticatedProvider", sessionAttributes.get("authenticatedProvider") )
                    sessionAttributes.put( "switchSourceAuthenticatedUser", username)
                elif (switchFlowStatus == "2_GET_TARGET"):
                    print "Passport. attemptAuthentication. SWITCH FLOW: Setting TARGET provider to %s" % sessionAttributes.get("authenticatedProvider")
                    sessionAttributes.put("switchTargetAuthenticatedProvider", sessionAttributes.get("authenticatedProvider") )
                    sessionAttributes.put("switchTargetAuthenticatedUser", username)
                elif (sessionAttributes.get("mfaFlowStatus") == "MFA_1_REQUIRED"):
                    print "Passport. attemptAuthentication. MFA FLOW: starting flow marking status = MFA_2_IN_PROGRESS"
                    sessionAttributes.put("mfaFlowStatus", "MFA_2_IN_PROGRESS")
                    identity.setWorkingParameter("selectedProvider", "mfa")

            return logged_in


    def getUserByExternalUid(self, uid, provider, userService):
        newFormat = "passport-%s:%s:%s" % ("saml", provider, uid)
        user = userService.getUserByAttribute("oxExternalUid", newFormat)

        if user == None:
            oldFormat = "passport-%s:%s" % ("saml", uid)
            #user = userService.getUserByAttribute("oxExternalUid", oldFormat)

            if user != None:
                # Migrate to newer format
                list = HashSet(user.getAttributeValues("oxExternalUid"))
                list.remove(oldFormat)
                list.add(newFormat)
                user.setAttribute("oxExternalUid", ArrayList(list))
                print "Migrating user's oxExternalUid to newer format 'passport-saml:provider:uid'"
                userService.updateUser(user)

        return user


    def setMessageError(self, msg, severity):
        facesMessages = CdiUtil.bean(FacesMessages)
        facesMessages.setKeepMessages()
        facesMessages.clear()
        facesMessages.add(severity, msg)


    def checkRequiredAttributes(self, profile, attrs):

        for attr in attrs:
            if (not attr in profile) or len(profile[attr]) == 0:
                print "Passport. checkRequiredAttributes. Attribute '%s' is missing in profile" % attr
                return False
        return True


    def addUser(self, externalUid, profile, userService):

        newUser = User()
        #Fill user attrs
        newUser.setAttribute("oxExternalUid", externalUid)
        self.fillUser(newUser, profile)
        newUser = userService.addUser(newUser, True)
        return newUser


    def updateUser(self, foundUser, profile, userService):
        # when this is false, there might still some updates taking place (e.g. not related to profile attrs released by external provider)
        if (not self.skipProfileUpdate):
            self.fillUser(foundUser, profile)
        userService.updateUser(foundUser)


    def fillUser(self, foundUser, profile):
    
        for attr in profile:
            # "provider" is disregarded if part of mapping
            if attr != self.providerKey:
                values = profile[attr]
                # COLLECT - here go through existing PersistentIDs add new ones for RPs that if they are not found
                print "Passport. fillUser. %s = %s" % (attr, values)
                if attr == "persistentId":
                    if (values != None):
                        # There is only one value from the mapping
                        newPersistenId = values[0]
                        # The format is rp|idp|uid, so we split by '|' and take the first element of the array
                        currentRp = StringHelper.split(values[0],'|')[0]
                        # then we look through the old values if there is a matching RP remove if from "values" and do not update
                        userPersistentIds = foundUser.getAttributeValues("persistentId")
                        if (userPersistentIds != None):
                            for userPersistentId in userPersistentIds:
                                if ( userPersistentId.find(currentRp) > -1 ):
                                    values.pop(0)
                        
                        # if there still is a persistentId, then add it to the current user profile
                        if ( len(values) > 0):
                            print "Passport. fillUser. Updating persistent IDs, original = '%s'" % userPersistentIds
                            # if there are no current Persistent IDs create a new list
                            tmpList = ArrayList(userPersistentIds) if userPersistentIds != None else ArrayList()
                            tmpList.add(newPersistenId)
                            print "Passport. fillUser. Updating persistent IDs, updated  = '%s'" % tmpList
                            foundUser.setAttribute(attr, tmpList)
                        else:
                            print "Passport. fillUser. PersistentId for RP '%s' already exists, ignoring new RP mapping" % currentRp

                elif attr == "mail":
                    oxtrustMails = []
                    for mail in values:
                        oxtrustMails.append('{"value":"%s","primary":false}' % mail)
                    foundUser.setAttribute("oxTrustEmail", oxtrustMails)

                else:
                    foundUser.setAttribute(attr, values)

# IDP-initiated flow routines

    def isInboundFlow(self, identity):
        sessionId = identity.getSessionId()
        if sessionId == None:
            # Detect mode if there is no session yet. It's needed for getPageForStep method
            facesContext = CdiUtil.bean(FacesContext)
            requestParameters = facesContext.getExternalContext().getRequestParameterMap()

            authz_state = requestParameters.get(AuthorizeRequestParam.STATE)
        else:
            authz_state = identity.getSessionId().getSessionAttributes().get(AuthorizeRequestParam.STATE)

        if self.isInboundJwt(authz_state):
            return True

        return False


    def isInboundJwt(self, value):
        if value == None:
            return False

        try:
            jwt = Jwt.parse(value)
            user_profile_json = jwt.getClaims().getClaimAsString("data")
            if StringHelper.isEmpty(user_profile_json):
                return False
        except:
            return False

        return True