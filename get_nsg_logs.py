import os, uuid, sys
import json
import argparse
from azure.storage.blob import BlobServiceClient
from datetime import datetime, timezone
import re
import time
import pandas as pd

# Get start time
start_time = time.time()

# Get input arguments
parser = argparse.ArgumentParser(description='Get the latest flow logs in a storage account')
parser.add_argument('--account-name', dest='account_name', action='store',
                    help='you need to supply an storage account name. You can get a list of your storage accounts with this command: az storage account list -o table')
parser.add_argument('--display-lb', dest='display_lb', action='store_true',
                    default=False,
                    help='display or hide flows generated by the Azure LB (default: False)')
parser.add_argument('--display-allowed', dest='display_allowed', action='store_true',
                    default=False,
                    help='display as well flows allowed by NSGs (default: False)')
parser.add_argument('--display-direction', dest='display_direction', action='store', default='in',
                    help='display flows only in a specific direction. Can be in, out, or both (default both)')
parser.add_argument('--display-hours', dest='display_hours', action='store', type=int, default=1,
                    help='How many hours to look back (default: 1)')
parser.add_argument('--display-minutes', dest='display_minutes', action='store', type=int, default=0,
                    help='How many minutes to look back (default: 0/unlimited)')
parser.add_argument('--only-non-zero', dest='only_non_zero', action='store_true',
                    default=False,
                    help='display only v2 flows with non-zero packet/byte counters (default: False)')
parser.add_argument('--no-counters', dest='no_counters', action='store_true',
                    default=False,
                    help='dont desplay any packet/byte counters (default: False)')
parser.add_argument('--flow-state', dest='flow_state_filter', action='store',
                    help='filter the output to a specific v2 flow type (B/C/E)')
parser.add_argument('--ip', dest='ip_filter', action='store',
                    help='filter the output to a specific IP address')
parser.add_argument('--ip2', dest='ip2_filter', action='store',
                    help='additional IP address filter')
parser.add_argument('--port', dest='port_filter', action='store',
                    help='filter the output to a specific TCP/UDP port')
parser.add_argument('--protocol', dest='protocol_filter', action='store',
                    help='filter the output to a specific protocol (T/U/I)')
parser.add_argument('--resource-name', dest='resource_name_filter', action='store',
                    help='filter the output to a specific NSG or Firewall')
parser.add_argument('--mode', dest='mode', action='store', default='nsg',
                    help='can be nsg,fw,both (default: nsg)')
parser.add_argument('--vnet-flow-logs', dest='vnet_flow_logs', action='store_true',
                    default=False,
                    help='Look for VNet Flow Logs are analyzed (default: False)')
parser.add_argument('--aggregate', dest='aggregate', action='store_true',
                    default=False,
                    help='prints byte/packet count aggregates (default: False)')
parser.add_argument('--no-output', dest='no_output', action='store_true',
                    default=False,
                    help='does not print out any output, useful with --verbose flag (default: False)')
parser.add_argument('--verbose', dest='verbose', action='store_true',
                    default=False,
                    help='run in verbose mode (default: False)')
args = parser.parse_args()

# Set to true if only packet drops should be displayed
display_only_drops = not args.display_allowed

# How many blobs to inspect (in an ordered list, there is one blob per hour)
display_hours = args.display_hours
display_minutes = args.display_minutes

# Set to False if you dont want to see traffic generated by the Azure Load Balancer
display_lb = args.display_lb

# Set to "in", "out" or "both"
display_direction = args.display_direction
if not display_direction in set(['in', 'out', 'both']):
    print('Please see this script help about how to set the --display-direction argument, only in|out|both supported')
    exit(1)

# Validation for mode
if not args.mode in set(["nsg", "fw", "both"]):
    print('Please see this script help about how to set the --mode argument, only nsg|fw|both supported')
    exit(1)

# Setting storage account name and key
account_name = args.account_name
try:
    account_key = os.environ.get('STORAGE_ACCOUNT_KEY')
except:
    print('The environment variable STORAGE_ACCOUNT_KEY does not exist. You can create it with this command: export STORAGE_ACCOUNT_KEY=$(az storage account keys list -n your_storage_account_name --query [0].value -o tsv)')
    exit(1)
if account_key == None:
    print('The environment variable STORAGE_ACCOUNT_KEY does not exist. You can create it with this command: export STORAGE_ACCOUNT_KEY=$(az storage account keys list -n your_storage_account_name --query [0].value -o tsv)')
    exit(1)

# Print filter information
if args.verbose:
    print('DEBUG: Storage account:', account_name)
    if args.ip_filter:
        print("Filtering to IP", args.ip_filter)
    if args.port_filter:
        print("Filtering to port", args.port_filter)
    if args.flow_state_filter:
        print("Filtering to flow state", args.flow_state_filter)
    print('DEBUG: Display variables: display_lb:', display_lb, '- display_direction:', display_direction, '- display_hours:', display_hours, '- display_only_drops:', display_only_drops)

# Initialize aggregate variables for NSG count
packets_src_to_dst_aggr = 0
bytes_src_to_dst_aggr = 0
packets_dst_to_src_aggr = 0
bytes_dst_to_src_aggr = 0

# The container will be the same for v1 and v2 NSG flow logs, but different for VNet Flow Logs
if args.vnet_flow_logs:
    flowlogs_container_name = "insights-logs-flowlogflowevent"
else:
    flowlogs_container_name = "insights-logs-networksecuritygroupflowevent"
fw_container_name = "insights-logs-azurefirewall"


#############
# Functions #
#############

def get_blob_client(account_name, account_key):
    try:
        return BlobServiceClient(account_name + ".blob.core.windows.net", credential=account_key)
    except Exception as e:
        print("ERROR: Could not create the blob service client to storage account", storage_account, '-', str(e))
        exit(1)

def get_container_client(block_blob_service, container_name):
    try:
        return block_blob_service.get_container_client(container_name)
    except Exception as e:
        print("ERROR: Could not create container client for container", container_name, '-', str(e))
        return None

def get_blob_list(container_client):
    try:
        return list(container_client.list_blobs())
    except Exception as e:
        print("ERROR: Error fetching blob list from container", str(e))
        return None

def get_resource_list(blob_list):
    # Get a list of resources
    resource_list = set([])
    num_of_blobs=0
    num_of_resources=0
    for this_blob in blob_list:
        # if args.verbose:
        #     print("DEBUG: blob found: {0}".format(this_blob['name']))
        blob_name_parts = this_blob.name.split('/')
        try:
            this_resource = blob_name_parts[8]
            if not this_resource in resource_list:
                resource_list.add(this_resource)
                num_of_resources += 1
            num_of_blobs += 1
        except:
            pass
    if args.verbose:
        print('DEBUG: Found', str(num_of_blobs), 'blobs for', str(num_of_resources), 'resources (FWs/NSGs).')
        print('DEBUG: resources found in that storage account:', resource_list)
    return resource_list

def process_fw_logs(data):
    # Variable that will be returned
    df_logs = pd.DataFrame()
    # log processing
    for log in data:
        if ('properties' in log) and ('msg' in log['properties']):
            resource_name = log['resourceId'].split('/')[8]
            logrow_dict = {
                'timestamp': [ pd.Timestamp(log['time']) ],
                'type': 'fw',
                'resource': resource_name
            }
            msg = log['properties']['msg']
            # Action
            action = re.findall(r'(?:Action\:)\s(\w*)', msg)
            if len(action) > 0:
                logrow_dict.update({'action': [ action[0][0] ] })
                # if args.verbose:
                #     print("DEBUG: Action {0} extracted from {1} in message {2}".format(action[0][0], str(action), msg))
            # elif args.verbose:
            #     print("DEBUG: No action could be extracted from msg", msg)
            # Src
            src_txt = re.findall(r'(?:from)\s(\S*)', msg)
            if len(src_txt) == 1:
                src_ip = re.findall(r'\d+\.\d+\.\d+\.\d+', src_txt[0])
                if len(src_ip) == 1:
                    logrow_dict.update({'src_ip': src_ip})
                    # if args.verbose:
                    #     print('DEBUG: IP addresses {0} found in src block {1}, dictionary is now {2}'.format(str(src_ip), src_txt[0], str(logrow_dict)))
                else:
                    if args.verbose:
                        print('DEBUG: no IP information found in src block', src_txt[0])
                src_port = re.findall(r'(?:\:)(\d+)', src_txt[0])
                if len(src_port) > 0:
                    logrow_dict.update({'src_port': src_port})
            else:
                if args.verbose:
                    print('DEBUG: no src information found in message', msg)
            # Dst
            dst_txt = re.findall(r'(?:to)\s(\S*)', msg)
            if len(dst_txt) == 1:
                dst_ip = re.findall(r'(\d+\.\d+\.\d+\.\d+)', dst_txt[0])
                if len(dst_ip) == 1:
                    logrow_dict.update({'dst_ip': dst_ip})
                else:
                    if args.verbose:
                        print('DEBUG: no IP information found in dst block', dst_txt[0])
                dst_port = re.findall(r'(?:\:)(\d+)', dst_txt[0])
                if (len(dst_port) == 1):
                    logrow_dict.update({'dst_port': dst_port})
            else:
                if args.verbose:
                    print('DEBUG: no dst information found in message', msg)
            # Protocol
            protocol = re.findall(r'^(.*)\s(?:request)', msg)
            if len(protocol) > 0:
                # if args.verbose:
                #     print("DEBUG: extracted protocol {0} from message {1}".format(protocol[0], msg))
                logrow_dict.update({'protocol': [ protocol[0][0] ]})
                if protocol[0][0] == "I":
                    logrow_dict.update({'src_port': [''], 'dst_port': ['']})
            # if args.verbose:
            #     print('DEBUG: converting dictionary to pandas dataframe:', str(logrow_dict))
            # Pad with empty NSG v2 flowlog fields
            logrow_dict.update({'state': [ '' ], 'packets_src_to_dst': [''], 'bytes_src_to_dst': [''], 'packets_dst_to_src': [''], 'bytes_dst_to_src': [''], 'direction': ['']})
            df_logrow = pd.DataFrame(logrow_dict)
            df_logs = pd.concat([df_logs, df_logrow], ignore_index=True)
        else:
            print('ERROR: No properties.msg found in log', str(log))

    # return
    return df_logs

def process_flowlog_records(data):
    # Variable that will be returned
    df_logs = pd.DataFrame()
    # Counters
    record_counter = 0
    flow_counter = 0
    process_start_time = time.time()
    # flowlog processing
    for record in data['records']:
        record_counter += 1
        if 'resourceId' in record:
            resource_name = record['resourceId'].split('/')[8]
        elif 'flowLogResourceID' in record:
            resource_name = record['flowLogResourceID'].split('/')[8]
        else:
            resource_name = "unknown"
        timestamp = pd.Timestamp(record['time'])
        if 'properties' in record:
            if ('Version' in record['properties']) and (record['properties']['Version'] == 1):
                flow_version = 1
            elif ('Version' in record['properties']) and (record['properties']['Version'] == 2):
                flow_version = 2
        elif ('flowLogVersion' in record) and (record['flowLogVersion'] == 3):
            flow_version = 3
        elif ('flowLogVersion' in record) and (record['flowLogVersion'] == 4):
            flow_version = 4
        else:
            flow_version = "unknown"
        if (flow_version == 1) or (flow_version == 2):
            for rule in record['properties']['flows']:
                rule_name = rule["rule"]
                for flow in rule['flows']:
                    flow_counter += 1
                    for flowtuple in flow['flowTuples']:
                        # Version 1
                        if flow_version == 1:
                            tuple_values = flowtuple.split(',')
                            src_ip=tuple_values[1]
                            dst_ip=tuple_values[2]
                            src_port=tuple_values[3]
                            dst_port=tuple_values[4]
                            protocol=tuple_values[5]
                            direction=tuple_values[6]
                            action=tuple_values[7]
                            logrow_dict = {
                                'timestamp': [ timestamp ],
                                'type': ['nsg'],
                                'resource': [resource_name],
                                'rule': [rule_name],
                                'src_ip': [ src_ip ],
                                'dst_ip': [ dst_ip ],
                                'src_port': [ src_port ],
                                'dst_port': [ dst_port ],
                                'protocol': [ protocol ],
                                'direction': [ direction ],
                                'action': [ action ]
                            }
                            df_logrow = pd.DataFrame(logrow_dict)
                            df_logs = pd.concat([df_logs, df_logrow], ignore_index=True)
                        # Version 2
                        else:
                            tuple_values = flowtuple.split(',')
                            src_ip=tuple_values[1]
                            dst_ip=tuple_values[2]
                            src_port=tuple_values[3]
                            dst_port=tuple_values[4]
                            protocol=tuple_values[5]
                            direction=tuple_values[6]
                            action=tuple_values[7]
                            try:
                                state=tuple_values[8]
                            except:
                                state=""
                            try:
                                packets_src_to_dst=tuple_values[9]
                            except:
                                packets_src_to_dst=""
                            try:
                                bytes_src_to_dst=tuple_values[10]
                            except:
                                bytes_src_to_dst=""
                            try:
                                packets_dst_to_src=tuple_values[11]
                            except:
                                packets_dst_to_src=""
                            try:
                                bytes_dst_to_src=tuple_values[12]
                            except:
                                bytes_dst_to_src=""
                            logrow_dict = {
                                'timestamp': [ timestamp ],
                                'type': ['nsg'],
                                'resource': [resource_name],
                                'rule': [rule_name],
                                'state': [ state ],
                                'packets_src_to_dst': [ packets_src_to_dst ],
                                'bytes_src_to_dst': [ bytes_src_to_dst ],
                                'packets_dst_to_src': [ packets_dst_to_src ],
                                'bytes_dst_to_src': [ bytes_dst_to_src ],
                                'src_ip': [ src_ip ],
                                'dst_ip': [ dst_ip ],
                                'src_port': [ src_port ],
                                'dst_port': [ dst_port ],
                                'protocol': [ protocol ],
                                'direction': [ direction ],
                                'action': [ action ]
                            }
                            df_logrow = pd.DataFrame(logrow_dict)
                            df_logs = pd.concat([df_logs, df_logrow], ignore_index=True)
        elif (flow_version == 4):
            for flow in record['flowRecords']['flows']:
                aclId = flow['aclID']
                for flowGroup in flow['flowGroups']:
                    flow_counter += 1
                    rule_name = flowGroup["rule"]
                    for flowtuple in flowGroup['flowTuples']:
                        tuple_values = flowtuple.split(',')
                        src_ip=tuple_values[1]
                        dst_ip=tuple_values[2]
                        src_port=tuple_values[3]
                        dst_port=tuple_values[4]
                        protocol=tuple_values[5]
                        direction=tuple_values[6]
                        state=tuple_values[7]
                        action="A"
                        try:
                            packets_src_to_dst=tuple_values[9]
                        except:
                            packets_src_to_dst=""
                        try:
                            bytes_src_to_dst=tuple_values[10]
                        except:
                            bytes_src_to_dst=""
                        try:
                            packets_dst_to_src=tuple_values[11]
                        except:
                            packets_dst_to_src=""
                        try:
                            bytes_dst_to_src=tuple_values[12]
                        except:
                            bytes_dst_to_src=""
                        logrow_dict = {
                            'timestamp': [ timestamp ],
                            'type': ['nsg'],
                            'resource': [resource_name],
                            'rule': [rule_name],
                            'state': [ state ],
                            'packets_src_to_dst': [ packets_src_to_dst ],
                            'bytes_src_to_dst': [ bytes_src_to_dst ],
                            'packets_dst_to_src': [ packets_dst_to_src ],
                            'bytes_dst_to_src': [ bytes_dst_to_src ],
                            'src_ip': [ src_ip ],
                            'dst_ip': [ dst_ip ],
                            'src_port': [ src_port ],
                            'dst_port': [ dst_port ],
                            'protocol': [ protocol ],
                            'direction': [ direction ],
                            'action': [ action ]
                        }
                        df_logrow = pd.DataFrame(logrow_dict)
                        df_logs = pd.concat([df_logs, df_logrow], ignore_index=True)
        else:
            print("ERROR: Flow version", flow_version, "not supported")
    if args.verbose:
        print("DEBUG: {0} records and {1} flows added to data frame in {2} seconds".format(record_counter, flow_counter, time.time()-process_start_time))
    return df_logs

# Go over each provided resource (NSG or FW), get the blobs, and send each blob to the corresponding processing routing (NSG or FW)
def process_resources (resource_list, blob_list, container_client):
    # Variable that will be returned
    df_logs = pd.DataFrame()
    # Process each resource (NSG/FW)
    for resource in resource_list:
        # Get a list of days for a given resource (NSG/FW)
        # List comprehensions do not seem to work (TypeError: 'ListGenerator' object is not subscriptable)
        # dayList = [nsg_blob_list[i].split('/')[11] for i in nsg_blob_list if nsg_blob_list[i].split('/')[8] == nsg_name]

        # Check NSG filter
        if (not args.resource_name_filter) or (resource.lower() == args.resource_name_filter.lower()):
            date_list = []
            for this_blob in blob_list:
                blob_name_parts = this_blob.name.split('/')
                try:
                    blob_resource = blob_name_parts[8]
                    blob_time = "/".join(blob_name_parts[9:14])
                    if blob_resource == resource:
                        date_list.append(blob_time)
                except:
                    pass
            date_list = list(set(date_list))  # Remove duplicates
            full_date_list = sorted(date_list, reverse=True)
            filtered_date_list = full_date_list[:display_hours]
            if args.verbose:
                print('DEBUG: Hourly blobs found for resource', resource, ':', filtered_date_list, '- display_hours: ', display_hours)
                print('DEBUG: Full date list for resource', resource, ':', full_date_list)
            for thisDate in filtered_date_list:
                # Get the matching blobs for a given resource and date
                blob_matches = []
                for this_blob in blob_list:
                    blob_name_parts = this_blob.name.split('/')
                    try:
                        blob_resource  = blob_name_parts[8]
                        blob_time = "/".join(blob_name_parts[9:14])
                        if blob_resource == resource and blob_time == thisDate:
                            blob_matches.append(this_blob.name)
                    except:
                        pass

                # Now we have a list of blobs that we want to process
                for blob_name in blob_matches:
                    if args.verbose:
                        print('DEBUG: Reading blob', blob_name)
                    local_filename = "/tmp/blob.json"
                    if os.path.exists(local_filename):
                        os.remove(local_filename)
                    blob_client = container_client.get_blob_client(blob_name)
                    if args.verbose:
                        print('DEBUG: Blob has', str(blob_client.get_blob_properties().size), 'bytes')
                    with open(local_filename, "wb") as download_file:
                        download_file.write(blob_client.download_blob().readall())
                    # We load each blob as text
                    text_data=open(local_filename).read()

                    # If the file starts with '{ "category"', it needs to be converted to proper JSON before transforming to an object
                    if text_data[:12] == "{ \"category\"":
                        if args.verbose:
                            print("DEBUG: converting sequence of JSON dictionaries to array")
                        data = []
                        text_lines = text_data.splitlines()
                        for text_line in text_lines:
                            try:
                                data.append(json.loads(text_line))
                            except:
                                print("Could not process JSON line:", text_line)
                                exit(1)
                    else:
                        if args.verbose:
                            print("DEBUG: converting JSON text to object...")
                        try:
                            data = json.loads(text_data)
                        except:
                            print("Could not process JSON file:", text_data)
                            exit(1)
                    
                    # Now that we have a JSON object, processing depends on the format of logs
                    # If there is a 'records' field, it is flow_logs
                    if 'records' in data:
                        df_logs = pd.concat([df_logs, process_flowlog_records(data)], ignore_index=True)
                    # otherwise we assume fw logs
                    else:
                        # if args.verbose:
                        #     print('DEBUG: text data is {0} characters long'.format(str(len(text_data))))
                        #     print(text_data)
                        #     print('DEBUG: JSON data is {0} long'.format(str(len(data))))
                        df_logs = pd.concat([df_logs, process_fw_logs(data)], ignore_index=True)
    return df_logs

#########
# Start #
#########

# Initialize dataframe
df_logs = pd.DataFrame()

# Connect to storage account
block_blob_service = get_blob_client(account_name, account_key)

# Create clients and get blob lists
if (args.mode == 'nsg') or (args.mode == 'both'):
    nsg_container_client = get_container_client(block_blob_service, flowlogs_container_name)
    nsg_blob_list = get_blob_list(nsg_container_client)
    if nsg_blob_list:
        nsg_list = get_resource_list (nsg_blob_list)
        nsg_logs = process_resources (nsg_list, nsg_blob_list, nsg_container_client)
        df_logs = pd.concat([df_logs, nsg_logs], ignore_index=True)

if (args.mode == 'fw') or (args.mode == 'both'):
    fw_container_client = get_container_client(block_blob_service, fw_container_name)
    fw_blob_list = get_blob_list(fw_container_client)
    if fw_blob_list:
        fw_list = get_resource_list (fw_blob_list)
        fw_logs = process_resources (fw_list, fw_blob_list, fw_container_client)
        df_logs = pd.concat([df_logs, fw_logs], ignore_index=True)

# Filter dataframe
if len(df_logs)>0:
    try:
        if (args.verbose):
            print("DEBUG: sorting dataframe...")
        df_logs = df_logs.sort_values(by='timestamp')
    except Exception as e:
        print("ERROR: Error sorting dataframe with timestamp column:", str(e))
        pass
    if (args.verbose):
        print("DEBUG: filtering dataframe...")
    try:
        if not display_lb:
            df_logs = df_logs[df_logs['src_ip'] != '168.63.129.16']
    except Exception as e:
        print("ERROR: Error filtering out LB IP addresses:", str(e))
        pass
    if display_only_drops:
        df_logs = df_logs[df_logs['action'] == 'D']
    if args.flow_state_filter:
        df_logs = df_logs[(df_logs['state'] == args.flow_state_filter) | (df_logs['type'] != 'nsg')]
    if display_direction and not (display_direction == "both"):
        if display_direction == "in":
            df_logs = df_logs[(df_logs['direction'] == 'I') | (df_logs['type'] != 'nsg')]
        elif display_direction == "out":
            df_logs = df_logs[(df_logs['direction'] == 'O') | (df_logs['type'] != 'nsg')]
    if args.port_filter:
        df_logs = df_logs[df_logs['dst_port'] == args.port_filter]
    if (args.ip_filter and not args.ip2_filter) or (not args.ip_filter and args.ip2_filter):
        if args.ip_filter:
            ip_filter=args.ip_filter
        else:
            ip_filter=args.ip2_filter
        df_logs = df_logs[(df_logs['src_ip'] == ip_filter) | (df_logs['dst_ip'] == ip_filter)]
    if args.ip_filter and args.ip2_filter:
        df_logs = df_logs[((df_logs['src_ip'] == args.ip_filter) & (df_logs['dst_ip'] == args.ip2_filter)) | ((df_logs['src_ip'] == args.ip2_filter) & (df_logs['dst_ip'] == args.ip_filter))]
    if args.ip_filter:
        df_logs = df_logs[(df_logs['src_ip'] == args.ip_filter) | (df_logs['dst_ip'] == args.ip_filter)]
    if args.protocol_filter:
        df_logs = df_logs[df_logs['protocol'] == args.protocol_filter]
    if args.only_non_zero:
        # df_logs = df_logs[(len(df_logs['bytes_src_to_dst']) > 0) & (len(df_logs['packets_src_to_dst']) > 0) & (len(df_logs['bytes_dst_to_src']) > 0) & (len(df_logs['packets_dst_to_src']) > 0)]
        df_logs = df_logs[((df_logs['bytes_src_to_dst'].notnull()) & (df_logs['packets_src_to_dst'].notnull()) & (df_logs['bytes_dst_to_src'].notnull()) & (df_logs['packets_dst_to_src'].notnull())) | (df_logs['type'] != 'nsg')]
    if args.no_counters:
        non_counter_cols = [col for col in df_logs.columns if (not col.startswith('packets')) and (not col.startswith('bytes'))]
        df_logs = df_logs[non_counter_cols]
    if args.display_minutes:
        # timestamp_limit = (pd.Timestamp.now()).tz_localize('UTC') - pd.Timedelta(args.display_minutes, 'minutes')
        timestamp_limit = (pd.Timestamp.utcnow()) - pd.Timedelta(args.display_minutes, 'minutes')
        if args.verbose:
            print("DEBUG: filtering logs more recent than {0}".format(str(timestamp_limit)))
        # timestamp_limit = pd.to_datetime(timestamp_limit)
        df_logs = df_logs[df_logs['timestamp'] > timestamp_limit ]

# Debug info on dataframe
if (args.verbose):
    print("DEBUG: dataframe shape (after filtering):", str(df_logs.shape))
    # Only print head if no other output selected
    if args.no_output:
        print("DEBUG: dataframe first rows:")
        print(df_logs.head())

# Output to screen
if not args.no_output:
    # Set pandas display options
    pd.set_option('display.max_rows', None)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 1000)
    pd.set_option('display.colheader_justify', 'center')
    pd.set_option('display.precision', 3)
    # Print
    if len(df_logs)>0:
        print(df_logs)
        # Print aggregates if required
        if args.aggregate:
            cols = ['bytes_src_to_dst', 'packets_src_to_dst', 'bytes_dst_to_src', 'packets_dst_to_src']
            print(df_logs[cols].sum(axis=0))
    else:
        print('No logs satisfy your filters, try other options')

# Print elapsed time
if args.verbose:
    print('DEBUG: Execution time:', time.time() - start_time , 'seconds')
