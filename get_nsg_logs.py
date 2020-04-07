import os, uuid, sys
import json
import argparse
from azure.storage.blob import BlockBlobService, PublicAccess


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
                    help='display flows only in a specific direction. Can be in, out, or both (default in)')
parser.add_argument('--display-hours', dest='display_hours', action='store', type=int, default=1,
                    help='How many hours to look back (default: 1)')
parser.add_argument('--version', dest='version', action='store', type=int, default=1,
                    help='NSG flow log version (1 or 2, default: 1)')
parser.add_argument('--only-non-zero', dest='only_non_zero', action='store_true',
                    default=False,
                    help='display only v2 flows with non-zero packet/byte counters (default: False)')
parser.add_argument('--flow-state', dest='flow_state_filter', action='store',
                    help='filter the output to a specific v2 flow type (B/C/E)')
parser.add_argument('--ip', dest='ip_filter', action='store',
                    help='filter the output to a specific IP address')
parser.add_argument('--port', dest='port_filter', action='store',
                    help='filter the output to a specific TCP/UDP port')
parser.add_argument('--nsg-name', dest='nsg_name_filter', action='store',
                    help='filter the output to a specific NSG')
parser.add_argument('--aggregate', dest='aggregate', action='store_true',
                    default=False,
                    help='run in verbose mode (default: False)')
parser.add_argument('--verbose', dest='verbose', action='store_true',
                    default=False,
                    help='run in verbose mode (default: False)')
args = parser.parse_args()

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
if args.verbose:
    print('DEBUG: Storage account:', account_name)

# Print filter information
if args.verbose:
    if args.ip_filter:
        print("Filtering to IP", args.ip_filter)
    if args.port_filter:
        print("Filtering to port", args.port_filter)
    if args.flow_state_filter:
        print("Filtering to flow state", args.flow_state_filter)

# Initialize aggregate variables
packets_src_to_dst_aggr = 0
bytes_src_to_dst_aggr = 0
packets_dst_to_src_aggr = 0
bytes_dst_to_src_aggr = 0

# The container will be the same for v1 and v2
container_name = "insights-logs-networksecuritygroupflowevent"

# Set to true if only packet drops should be displayed
display_only_drops = not args.display_allowed

# Set to "in", "out" or "both"
display_direction = args.display_direction
if not display_direction in set(['in', 'out', 'both']):
    print('Please see this script help about how to set the display_direction argument')

# Set to False if you dont want to see traffic generated by the Azure Load Balancer
display_lb = args.display_lb

# How many blobs to inspect (in an ordered list, there is one blob per minute)
display_hours = args.display_hours

block_blob_service = BlockBlobService(account_name=account_name, account_key=account_key)

try:
    blobList = block_blob_service.list_blobs(container_name)
except:
    print("Container", container_name, "does not seem to exist?")
    exit(1)

if args.verbose:
    print('DEBUG: Display variables: display_lb:', display_lb, '- display_direction:', display_direction, '- display_hours:', display_hours, '- display_only_drops:', display_only_drops)

# Get a list of NSGs
# List comprehension does not seem to work (TypeError: 'ListGenerator' object is not subscriptable)
# nsgList = [blobList[i].name.split('/')[8] for i in blobList]
nsgList = set([])
for this_blob in blobList:
    blob_name_parts = this_blob.name.split('/')
    thisNsg = blob_name_parts[8]
    if not thisNsg in nsgList:
        nsgList.add(thisNsg)
if args.verbose:
    print('DEBUG: NSGs found in that storage account:', nsgList)

for nsg_name in nsgList:
    # Get a list of days for a given NSG
    # List comprehensions do not seem to work (TypeError: 'ListGenerator' object is not subscriptable)
    # dayList = [blobList[i].split('/')[11] for i in blobList if blobList[i].split('/')[8] == nsg_name]
    date_list = []
    for this_blob in blobList:
        blob_name_parts = this_blob.name.split('/')
        blob_nsg  = blob_name_parts[8]
        blob_time = "/".join(blob_name_parts[9:14])
        if blob_nsg == nsg_name:
            date_list.append(blob_time)
    date_list = sorted(date_list, reverse=True)
    date_list = date_list[:display_hours]
    if args.verbose:
        print('DEBUG: Hourly blobs found for NSG', nsg_name, ':', date_list, '- display_hours: ', display_hours)

    for thisDate in date_list:
        # Get the corresponding blob for a given NSG and date
        blob_matches = []
        for this_blob in blobList:
            blob_name_parts = this_blob.name.split('/')
            blob_nsg  = blob_name_parts[8]
            blob_time = "/".join(blob_name_parts[9:14])
            if blob_nsg == nsg_name and blob_time == thisDate:
                blob_matches.append(this_blob.name)

        for blob_name in blob_matches:
            if args.verbose:
                print('DEBUG: Reading blob', blob_name)
            local_filename = "flowlog_tmp.json"
            if os.path.exists(local_filename):
                os.remove(local_filename)
            block_blob_service.get_blob_to_path(container_name, blob_name, local_filename)
            text_data=open(local_filename).read()
            try:
                data = json.loads(text_data)
            except:
                print("Could not process JSON:", text_data)
                exit(1)
            for record in data['records']:
                for rule in record['properties']['flows']:
                    for flow in rule['flows']:
                        for flowtuple in flow['flowTuples']:
                            if args.version == 1:
                                tuple_values = flowtuple.split(',')
                                src_ip=tuple_values[1]
                                dst_ip=tuple_values[2]
                                src_port=tuple_values[3]
                                dst_port=tuple_values[4]
                                protocol=tuple_values[5]
                                direction=tuple_values[6]
                                action=tuple_values[7]
                                display_record = False
                                if action=='D' or not display_only_drops:
                                    if (direction == 'I' and display_direction == 'in') or (direction == 'O' and display_direction == 'out') or (display_direction == 'both'):
                                            if src_ip != "168.63.129.16" or display_lb == True:
                                                display_record = True
                                if display_record:
                                    print(record['time'], nsg_name, rule['rule'], action, direction, src_ip, src_port, dst_ip, dst_port)
                            else:
                                tuple_values = flowtuple.split(',')
                                print(str(tuple_values))  # DEBUG
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
                                display_record = False
                                if action=='D' or not display_only_drops:
                                    if (direction == 'I' and display_direction == 'in') or (direction == 'O' and display_direction == 'out') or (display_direction == 'both'):
                                        if src_ip != "168.63.129.16" or display_lb == True:
                                            counters_not_zero = (len(packets_src_to_dst) + len(packets_dst_to_src) + len(bytes_src_to_dst) + len(bytes_dst_to_src) > 0)
                                            if ((not args.only_non_zero) or counters_not_zero):
                                                if ((not args.flow_state_filter) or state.lower() == args.flow_state_filter.lower()):
                                                    if ((not args.ip_filter) or (src_ip == args.ip_filter or dst_ip == args.ip_filter)):
                                                        if ((not args.port_filter) or (src_port == args.port_filter or dst_port == args.port_filter)):
                                                            if ((not args.nsg_name_filter) or (nsg_name.lower() == args.nsg_name_filter.lower())):
                                                                display_record = True
                                if display_record:
                                    # if args.verbose:
                                    #     print('DEBUG: Flow-tuple:', flowtuple)
                                    traffic_info = 'src2dst: ' + packets_src_to_dst + '/' + bytes_src_to_dst + ' dst2src: ' + packets_dst_to_src + '/' + bytes_dst_to_src
                                    #traffic_info = 'src2dst: ' + packets_src_to_dst + '/' + bytes_src_to_dst + ' dst2src: ' + packets_dst_to_src + '/' + bytes_dst_to_src + ' - '+str(len(packets_src_to_dst))+'/'+str(len(bytes_src_to_dst))+'/'+str(len(packets_dst_to_src))+'/'+str(len(bytes_dst_to_src))
                                    if protocol=='T':
                                        protocol='tcp'
                                    else:
                                        protocol='udp'
                                    print(record['time'], nsg_name, rule['rule'], action, direction, src_ip, protocol, src_port, dst_ip, dst_port, state, traffic_info)
                                    if args.aggregate:
                                        # Convert counters to integer
                                        if packets_src_to_dst.isnumeric():
                                            packets_src_to_dst = int(packets_src_to_dst)
                                        else:
                                            packets_src_to_dst = 0
                                        if packets_dst_to_src.isnumeric():
                                            packets_dst_to_src = int(packets_dst_to_src)
                                        else:
                                            packets_dst_to_src = 0
                                        if bytes_src_to_dst.isnumeric():
                                            bytes_src_to_dst = int(bytes_src_to_dst)
                                        else:
                                            bytes_src_to_dst = 0
                                        if bytes_dst_to_src.isnumeric():
                                            bytes_dst_to_src = int(bytes_dst_to_src)
                                        else:
                                            bytes_dst_to_src = 0
                                        # Add to aggregates    
                                        packets_src_to_dst_aggr += int(packets_src_to_dst)
                                        bytes_src_to_dst_aggr += int(bytes_src_to_dst)
                                        packets_dst_to_src_aggr += int(packets_dst_to_src)
                                        bytes_dst_to_src_aggr += int(bytes_dst_to_src)
if (args.aggregate and args.version == 2):
    print('Totals src2dst ->', packets_src_to_dst_aggr, "packets and", bytes_src_to_dst_aggr, "bytes")
    print('Totals dst2src ->', packets_dst_to_src_aggr, "packets and", bytes_dst_to_src_aggr, "bytes")

