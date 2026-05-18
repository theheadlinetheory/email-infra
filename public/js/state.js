// ── Global State & Constants ──

var overviewData = null;
var acquisitionDataGlobal = null;
var inventoryData = null;
var clientsList = [];

var trendChart = null;
var currentTrendClientId = null;
var currentDetailClientName = '';

var acqCampaignsCache = null;
var setupPipelinePollInterval = null;
var newSetupPipelineType = 'generic';

var currentMode = localStorage.getItem('dashboardMode') || 'fulfillment';

var zmData = null;
var domData = null;
var syncData = null;
var pipelineData = null;

var realtimeChannel = null;
var assignmentInProgress = false;
var pipelinePollingInterval = null;

var ASSIGN_STEPS = [
    {id: 1, label: 'Creating SmartLead client'},
    {id: 2, label: 'Setting group tags'},
    {id: 3, label: 'Verifying client assignment'},
    {id: 4, label: 'Updating Zapmail domain tags'},
    {id: 5, label: 'Setting forwarding domain'},
    {id: 6, label: 'Updating Google Sheet'},
    {id: 7, label: 'Updating pipeline record'},
];

var DELETE_STEPS = [
    {id: 1, label: 'Removing from campaigns'},
    {id: 2, label: 'Deleting SmartLead accounts'},
    {id: 3, label: 'Cancelling Zapmail domains'},
    {id: 4, label: 'Updating Google Sheet'},
    {id: 5, label: 'Deleting SmartLead client'},
];

var TRANSITION_STEPS = [
    {id: 1, label: 'Setting up new SmartLead client'},
    {id: 2, label: 'Updating SmartLead tags'},
    {id: 3, label: 'Verifying client assignment'},
    {id: 4, label: 'Updating Zapmail domain tags'},
    {id: 5, label: 'Setting forwarding domain'},
    {id: 6, label: 'Updating Google Sheet'},
];

var GENERIC_STEP_LABELS = {
    wait_active: 'Mailboxes Active',
    smartlead_export: 'SmartLead Export',
    smartlead_verify: 'Verify Accounts',
    tag_assign: 'Tag & Assign',
    enable_warmup: 'Enable Warmup',
    complete: 'Complete'
};

var GENERIC_STEP_ORDER = ['wait_active', 'smartlead_export', 'smartlead_verify', 'tag_assign', 'enable_warmup', 'complete'];

var GENERIC_COMPLETED_MAP = {
    mailboxes_active: 'wait_active',
    smartlead_export: 'smartlead_export',
    smartlead_verified: 'smartlead_verify',
    tagged: 'tag_assign',
    warmup_enabled: 'enable_warmup'
};

var genericTrackerInterval = null;
