import os
import subprocess
import time
import shutil
import json
import mdstat
import socket
import psutil
import requests
import datetime
import yaml

# --- Global Variables and Configuration ---
# Extend the PATH for commonly used binaries within the initramfs environment.
os.environ['PATH'] = os.environ.get('PATH', '') + ':/usr/bin:/usr/sbin:/bin:/usr/local/bin'

# BOOTSTRAPPED_FILE: Path to the script that kexec will execute for the main system.
BOOTSTRAPPED_FILE = "/sysroot/.bootstrapped"

IGNITION_FILE       = "/ignition.json"
FS_INSTALLED_MARKER = ".filesystem_installed_marker"
BOOTSTRAPPED_MARKER = ".bootstrapped_marker"
SYSROOT             = "/sysroot"

# Declare IgnitionConfig as a global variable to be accessed across functions
IgnitionConfig = None

# Node config
NodeConfig = None

class AttrDict:
    """
    A class that allows dictionary keys to be accessed as object attributes.
    Handles nested dictionaries by converting them recursively.
    """
    def __init__(self, dictionary):
        for key, value in dictionary.items():
            if isinstance(value, dict):
                setattr(self, key, AttrDict(value))
            elif isinstance(value, list):
                # Handle lists of dictionaries
                setattr(self, key, [AttrDict(item) if isinstance(item, dict) else item for item in value])
            else:
                setattr(self, key, value)
    def __str__(self):
        """
        Returns a string representation of the AttrDict instance,
        mimicking a dictionary's string representation.
        """
        # Convert the AttrDict back to a regular dictionary for string representation
        return str(self.__to_dict())

    def __repr__(self):
        """
        Returns a more detailed string representation, useful for debugging.
        """
        return f"AttrDict({self.__to_dict()})"

    def __to_dict(self):
        """
        Helper method to recursively convert AttrDict and its nested AttrDicts
        back into standard dictionaries.
        """
        result = {}
        for key, value in self.__dict__.items():
            if isinstance(value, AttrDict):
                result[key] = value.__to_dict()
            elif isinstance(value, list):
                result[key] = [item.__to_dict() if isinstance(item, AttrDict) else item for item in value]
            else:
                result[key] = value
        return result

# --- Helper for Marker Files ---
def _create_marker_file(marker_name, sysroot_mounted=True):
    """
    Creates a simple marker file to indicate stage completion.
    If sysroot_mounted is True, the marker is written to /sysroot/tmp/.
    Otherwise, it's written to /tmp/ (initramfs tmpfs).
    """
    if sysroot_mounted:
        marker_dir = SYSROOT
        # Ensure /sysroot/tmp exists before writing
        os.makedirs(marker_dir, exist_ok=True)
        marker_path = os.path.join(marker_dir, marker_name)
    else:
        # For early stages before /sysroot is mounted, use initramfs /tmp
        marker_path = os.path.join("/tmp", marker_name)

    try:
        with open(marker_path, 'w') as f:
            f.write(f"Stage '{marker_name}' completed successfully at {time.ctime()} (PID {os.getpid()})\n")
        print(f"Marker file created: {marker_path}")
    except IOError as e:
        print(f"Warning: Could not create marker file {marker_path}: {e}", file=os.sys.stderr)

# --- Functions ---

def run_command(command, check_success=True, shell=True, capture_output=False, stderr_to_stdout=True):
    """
    Runs a shell command and optionally checks its success.

    Args:
        command (str or list): The command to execute.
        check_success (bool): If True, raises an exception if the command fails.
        shell (bool): If True, the command will be executed through the shell.
        capture_output (bool): If True, captures stdout and stderr.
        stderr_to_stdout (bool): If True and capture_output is True, redirects stderr to stdout.

    Returns:
        subprocess.CompletedProcess: The result of the command execution.
    """
    try:
        if capture_output:
            result = subprocess.run(command, shell=shell, check=check_success,
                                    capture_output=True, text=True)
        else:
            result = subprocess.run(command, shell=shell, check=check_success)
        return result
    except subprocess.CalledProcessError as e:
        print(f"Error: Command failed: {e.cmd}", file=os.sys.stderr)
        if capture_output:
            print(f"Output: {e.stdout}", file=os.sys.stderr) # stdout contains stderr if stderr_to_stdout
        if check_success:
            raise
        return None

def setup_initial_filesystems():
    """
    Mounts essential pseudo-filesystems required by the kernel and user space.
    These include /proc for process information and /sys for device information.
    """
    print("Mounting /proc and /sys...")
    try:
        run_command("mount -t proc none /proc")
        run_command("mount -t sysfs none /sys")
        return True
    except Exception as e:
        print(f"Error: Failed to mount initial filesystems: {e}", file=os.sys.stderr)
        return False

def load_kernel_modules():
    """
    Loads necessary kernel modules for hardware detection and functionality.
    Modules are crucial for storage, network, and input devices.
    """
    print("Loading essential kernel modules...")
    modules = [
        "usbhid", "ehci-hcd", "xhci-hcd",
        "virtio", "virtio_pci", "virtio_blk", "virtio_net", "virtio_scsi", "virtio_ring",
        "mlx5_core", "mlx5_en", "mlx5_ib", "mlx5_eswitch",
        "nvme", "nvme_core", "nvme_pci"
    ]
    for module in modules:
        try:
            run_command(f"modprobe {module}", check_success=False)
        except Exception:
            print(f"Warning: {module} module not found or failed to load.", file=os.sys.stderr)
    run_command("lsmod")
    return True

def create_dev_nodes():
    """
    Populates the /dev directory with device nodes using mdev, BusyBox's
    simple udev replacement. This is essential for the system to interact
    with hardware devices.
    """
    print("Creating /dev entries with mdev...")
    try:
        run_command("mount -t devtmpfs devtmpfs /dev")
    except Exception as exc:
        print(f"failed to mount /dev/ {exc}")
    run_command("/bin/mdev -s")

def read_ignition_file(ignition_dest_path=IGNITION_FILE):
    """
    Reads ignition.json and loads as json
    """
    global IgnitionConfig
    try:
        with open(ignition_dest_path, 'r') as f:
            IgnitionConfig = AttrDict(json.load(f))
            print(IgnitionConfig)
    except FileNotFoundError:
        print(f"Error: {ignition_dest_path} not found after Ignition run.", file=os.sys.stderr)
        return False
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse JSON from {ignition_dest_path}: {e}", file=os.sys.stderr)
        return False
    return True

def kexec_boot(bootstrap_path):
    """
    Executes a new kernel, effectively performing a "soft reboot" into
    another system without a full hardware reset.

    Args:
        bootstrap_path (str): The path to the bootstrap script or executable.
    Returns:
        bool: False if kexec fails (script will continue), does not return if successful.
    """
    if not bootstrap_path:
        print("Error: No bootstrap path provided to kexec_boot.", file=os.sys.stderr)
        return False
    print(f"Booting from {bootstrap_path} using kexec...")
    try:
        # The execve call replaces the current process, so this function won't return
        # if kexec is successful.
        os.execv(bootstrap_path, [bootstrap_path])
        # If execv fails, the following line will be reached.
        print(f"Error: kexec command failed to execute {bootstrap_path}. Check kexec logs.", file=os.sys.stderr)
        return False # Indicate failure if exec returns
    except OSError as e:
        print(f"Error during kexec_boot: {e}", file=os.sys.stderr)
        return False

def write_kexec_command(kernel_path, initramfs_path, kernel_cmdline):
    """
    Generates a kexec command and writes it to a specified target file.
    This file can then be executed by `kexec_boot` to load a new kernel.

    Args:
        kernel_path (str): Path to the kernel image (e.g., /boot/vmlinuz).
        initramfs_path (str): Path to the initramfs image (e.g., /boot/initrd.img).
        kernel_cmdline (str): Kernel command line arguments.

    Returns:
        bool: True on success, False on error.
    """
    target_file = os.path.join("/sysroot", BOOTSTRAPPED_MARKER)
    print(f"Attempting to write kexec command to {target_file}...")

    if not all([kernel_path, initramfs_path, kernel_cmdline]):
        print("Error: All three arguments (kernel_path, initramfs_path, kernel_cmdline) are required.", file=os.sys.stderr)
        print("Usage: write_kexec_command <kernel_path> <initramfs_path> \"<kernel_cmdline>\"", file=os.sys.stderr)
        return False

    if not os.path.isfile(kernel_path):
        print(f"Error: Kernel image not found at {kernel_path}.", file=os.sys.stderr)
        return False

    if not os.path.isfile(initramfs_path):
        print(f"Error: Initramfs image not found at {initramfs_path}.", file=os.sys.stderr)
        return False

    if not os.path.isdir("/sysroot"):
        print("Error: The /sysroot directory does not exist or is not a directory.", file=os.sys.stderr)
        print("This function assumes a chroot or similar environment where /sysroot is the target root.", file=os.sys.stderr)
        return False

    kexec_cmd = f'kexec -l "{kernel_path}" --initrd="{initramfs_path}" --append="{kernel_cmdline}"'

    try:
        with open(target_file, 'w') as f:
            f.write(f"#!/bin/bash\n") # Ensure it's a bash script for kexec
            f.write(f"{kexec_cmd}\n")
            f.write("kexec -e\n")
        os.chmod(target_file, 0o755)
        print(f"Successfully wrote kexec command to {target_file}.")
        print(f"Command written: {kexec_cmd}")
        print(f"Permissions set to 644 for {target_file}.")
        return True
    except IOError as e:
        print(f"Error: Failed to write kexec command to {target_file}: {e}", file=os.sys.stderr)
        print("Please ensure sufficient permissions (e.g., run with sudo).", file=os.sys.stderr)
        return False

def get_ignition_root():
    """
    Returns root fs entry from ignition config.
    """
    print(f"Reading root fs entry from ignition.json")
    for fs in IgnitionConfig.storage.filesystems:
        if fs.path == "/":
            return fs
    raise Exception("failed to find root filesystem")

def assemble_raid(fs):
    print(f"Probing for raid in {fs.device}")
    if "md" in fs.device: # Raided device
        for raid in IgnitionConfig.storage.raid:
            if fs.device.split("/")[-1] in raid.name:
                devices = " ".join(raid.devices)
                try:
                    mdstat_detail = mdstat.parse()
                    for raid, raid_detail in mdstat_detail["devices"].items():
                        if raid_detail["active"]:
                            print(f"Stopping raid /dev/{raid}")
                            run_command(f"mdadm --stop /dev/{raid}")

                    print(f"Assembling raid device {fs.device} with {devices}")
                    run_command(f"mdadm --assemble {fs.device} {devices}")
                    return True
                except subprocess.CalledProcessError as exc:
                    print(f"Failed to assemble raid, probably raid was not created.")
    return False

def get_filesystem_type(device_path):
    """
    Detects the filesystem type on a given block device using blkid.

    Args:
        device_path (str): The path to the block device (e.g., "/dev/sda1").

    Returns:
        str or None: The filesystem type (e.g., "ext4", "ntfs", "vfat") if found,
                     otherwise None.
    """
    try:
        # Run blkid command to get information about the device
        # -o export: output in a key="value" format
        # -s TYPE: only show the TYPE attribute
        result = subprocess.run(
            ['blkid', '-o', 'export', '-s', 'TYPE', device_path],
            capture_output=True,
            text=True,
            check=True
        )
        output = result.stdout.strip()
        print(f"DEBUG: blkid {output}")
        
        for line in output.splitlines():
            if line.startswith("TYPE="):
                # Extract the filesystem type
                fs_type = line.split('=')[1].strip('"')
                print(f"Found {fs_type} at {device_path}")
                return fs_type
        return None # No FSTYPE found, likely no filesystem
    except subprocess.CalledProcessError:
        # blkid returns non-zero exit code if no filesystem or device not found
        return None
    except FileNotFoundError:
        print(f"Error: 'blkid' command not found. Please ensure it's installed and in your PATH.")
        return None
        
def mount_root():
    """
    Mounts root fs at /sysroot
    """
    root_fs = get_ignition_root()
    assemble_raid(root_fs)

    # Device exists?
    if not os.path.exists(root_fs.device):
        return False

    # Filesystem created?
    if not get_filesystem_type(root_fs.device):
        return False

    run_command(f"mount -t {root_fs.format} {root_fs.device} {SYSROOT}")

    return True

def configure_network():
    """
    Attempts to bring up network interfaces and obtain an IP address via DHCP.
    """
    print("Probing for network interfaces and attempting DHCP...")
    run_command("ls /sys/class/net/", check_success=False) # ls might fail if /sys/class/net is empty

    os.makedirs("/var/lib/dhcp/", exist_ok=True)
    # BusyBox dhclient might need /var/run, which is often a symlink to /run.
    try:
        if not os.path.exists("/var/run"):
            os.symlink("/run", "/var/run")
    except FileExistsError:
        pass # Symlink already exists

    for interface_entry in os.listdir("/sys/class/net/"):
        if_name = os.path.basename(interface_entry)

        if if_name == "lo" or not if_name:
            continue

        print(f"Found network interface: {if_name}")
        print(f"Bringing interface {if_name} up...")
        run_command(f"ip link set dev {if_name} up")

        print(f"Attempting DHCP on {if_name} for up to 10 seconds...")
        try:
            # Using 'dhclient' (common in Buildroot) with a timeout.
            subprocess.run(f"timeout 10 dhclient -v {if_name}", shell=True, check=True)
            print(f"Successfully obtained IP on {if_name} using dhclient!")
            return True # Success
        except subprocess.CalledProcessError:
            print(f"DHCP failed on {if_name}. Bringing interface down.", file=os.sys.stderr)
            run_command(f"ip link set dev {if_name} down", check_success=False)

    print("Network probing complete. No network connection could be detected.")
    return False # Failure

def provision_storage():
    """
    Parses the ignition.json file to determine the root filesystem device
    and mounts it to /sysroot. This prepares the system for chrooting
    into the main operating system.
    """
    run_command("mkdir -p /run")

    ignition_source_path = "/ignition.json"
    ignition_dest_path = "/run/ignition.json"

    if not os.path.isfile(ignition_source_path):
        print(f"Error: {ignition_source_path} not found. Cannot determine root partition.", file=os.sys.stderr)
        return False
    try:
        shutil.copy(ignition_source_path, ignition_dest_path)
    except IOError as e:
        print(f"Error: Failed to copy {ignition_source_path} to {ignition_dest_path}: {e}", file=os.sys.stderr)
        return False

    print("Running /usr/bin/ignition to configure disks...")
    try:
        run_command("/usr/bin/ignition -platform file -stage disks")
    except subprocess.CalledProcessError:
        print("Error: Ignition disk stage failed.", file=os.sys.stderr)
        return False
    
    return True

def final_setup():
    """
    Performs final setup tasks before handing control to the main system,
    such as mounting /run as tmpfs and setting the umask.
    """
    print("Performing final setup steps...")
    try:
        run_command("mount -t tmpfs tmpfs /run -o mode=0755,nodev,nosuid", check_success=False)
    except Exception:
        print("WARNING: Failed to mount /run as tmpfs.", file=os.sys.stderr)

    os.umask(0o077)
    print("Umask set to 077.")
    return True

def transfer_rootfs(source="rsync://10.10.6.5/images/k8s-worker-dgx-h200-image-060525/*",
                    destination="/sysroot", max_retries=5, delay=10):
    """
    Transfers the root filesystem from a remote source using rsync.
    Note: This function is commented out by default in main.
          Uncomment it in main to enable rootfs transfer.
    """
    rsync_options = "-azP --info=progress2,name0 --no-inc-recursive"

    retry_count = 0
    while True:
        print(f"Attempt {retry_count + 1} of {max_retries}...")
        try:
            # Use subprocess.run for rsync, as it might be a long-running process
            subprocess.run(f"rsync {rsync_options} {source} {destination}", shell=True, check=True)
            print("rsync completed successfully.")
            break
        except subprocess.CalledProcessError as e:
            print(f"rsync failed with exit status: {e.returncode}", file=os.sys.stderr)
            retry_count += 1

            if retry_count >= max_retries:
                print(f"Max retries ({max_retries}) reached. Rsync failed definitively.", file=os.sys.stderr)
                raise # Re-raise the last exception, indicating a critical failure
            
            current_delay = delay
            print(f"Retrying in {current_delay} seconds...", file=os.sys.stderr)
            time.sleep(current_delay)

    _create_marker_file(FS_INSTALLED_MARKER)
    return True # Indicate success of transfer if loop breaks

def get_raid_uuid(device):
    """
    Read raid uuid using madadm command
    """
    try:
        output = run_command(f"mdadm --detail {device} | grep -i uuid", capture_output=True)
        uuid = "-".join(output.stdout.split(":")[1:]).strip()
        return uuid
    except subprocess.CalledProcessError as e:
        print(f"failed to get raid uuid")
    return None

def generate_bootstrapped_file():
    """
    Generates the .bootstrapped file with kexec commands for the main system.
    Relies on `IgnitionConfig` being populated by `mount_root_partition`.
    """
    global NodeConfig

    print(f"Generating bootstrapped file")
    root_uuid = ""
    # Extract UUID from the global `IgnitionConfig`
    root_fs = get_ignition_root()
    root_uuid = root_fs.uuid
    
    if root_uuid:
        print(f"Identified root UUID: {root_uuid}")
    else:
        print("Warning: Could not determine root UUID for kexec command line.", file=os.sys.stderr)

    kexec_cmdline = NodeConfig["kernel_arguments"]
    
    if "md" in root_fs.device:
        raid_uuid = get_raid_uuid(root_fs.device)
        if not raid_uuid:
            raise Exception(f"failed to read raid uuid for {root_fs.device}")
        kexec_cmdline = f"{kexec_cmdline} rd.md=1 rd.md.auto=1 rd.md.uuid={raid_uuid} "
    
    if root_uuid:
        kexec_cmdline = f"{kexec_cmdline} root=UUID={root_uuid} "

    else:
        # Fallback to device path if UUID is not available.
        # This requires re-extracting the device path from `IgnitionConfig` or storing it.
        # Given `IgnitionConfig` is global, we can try to find it.
        root_disk_fallback = None
        if 'storage' in IgnitionConfig and 'filesystems' in IgnitionConfig['storage']:
            for fs in IgnitionConfig['storage']['filesystems']:
                if fs.get('path') == '/':
                    root_disk_fallback = fs.get('device')
                    break
        
        if root_disk_fallback:
            print(f"Warning: Using device path ({root_disk_fallback}) for root, consider providing UUID for robustness.", file=os.sys.stderr)
            kexec_cmdline = f"{kexec_cmdline} root={root_disk_fallback}"
        else:
            print("Critical: Neither UUID nor device path found for root. Kexec command line for root will be incomplete.", file=os.sys.stderr)
            return False # Cannot generate a reliable kexec command without root info

    if not write_kexec_command("/tmp/vmlinuz", "/tmp/initrd.img", kexec_cmdline):
        print("Error: Failed to write kexec command.", file=os.sys.stderr)
        return False
    return True

def get_my_ip():
    """
    """
    kernel_args = read_proc_cmdline()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = int(kernel_args["nodeconfigserverport"])
        s.connect((kernel_args["nodeconfigserver"], port)) # Connect to a public DNS server to get local IP
        my_local_ip = s.getsockname()[0]
        s.close()
        return my_local_ip
    except socket.error:
       raise

def get_mac_address_for_ip(target_ip):
    """
    Finds the MAC address of the network interface that has the given IP address.

    Args:
        target_ip (str): The IP address to search for.

    Returns:
        str: The MAC address (e.g., '00:11:22:33:44:55') or None if not found.
    """
    for interface_name, snics in psutil.net_if_addrs().items():
        for snic in snics:
            # Check for IPv4 address and if it matches the target_ip
            if snic.family == socket.AF_INET and snic.address == target_ip:
                # Once the IP is matched, look for the MAC address (AF_LINK)
                for mac_snic in psutil.net_if_addrs().get(interface_name, []):
                    if mac_snic.family == psutil.AF_LINK: # AF_LINK is for MAC address
                        return mac_snic.address.replace('-', ':').lower() # Format to common MAC style
    return None

def read_node_configuration():
    """
    Reads node configuration from nodeconfigserver specified on kernel command line
    """
    kernel_args = read_proc_cmdline()
    node_config_server = kernel_args["nodeconfigserver"]
    node_config_server_port = kernel_args["nodeconfigserverport"]
    mac = get_mac_address_for_ip(get_my_ip())
    for retry in range(0, 5):
        response = requests.get(f"http://{node_config_server}:{node_config_server_port}/nodes/{mac}.json")
        response.raise_for_status()
        if 'application/json' in response.headers.get('Content-Type', ''):
            return response.json()
    return None

def read_proc_cmdline():
    """
    Returns map of all arguments in cmdline that has "="
    """
    config = {}
    with open("/proc/cmdline") as inf:
        tokens = inf.read().split()
        print(tokens)
        for token in tokens:
            if "=" in token:
                ents = token.split("=")
                config[ents[0]] = ents[1]
    print(config)
    return config

import os
import datetime

def compare_file_mtime_with_unix_timestamp(file_path, unix_timestamp_str):
    """
    Compares a file's modified timestamp with a given Unix timestamp string.

    Args:
        file_path (str): The path to the file.
        unix_timestamp_str (str): A string representing a Unix timestamp.

    Returns:
        str: A string indicating the comparison result, or an error message.
    """
    try:
        # 1. Get the file's modified timestamp
        file_mtime_unix = os.path.getmtime(file_path)
        print(f"File '{file_path}' modified Unix timestamp: {file_mtime_unix}")
        print(f"File '{file_path}' modified datetime: {datetime.datetime.fromtimestamp(file_mtime_unix)}")


        # 2. Convert the given Unix timestamp string to an integer
        given_unix_timestamp = int(unix_timestamp_str)
        print(f"Given Unix timestamp string: {unix_timestamp_str}")
        print(f"Given Unix timestamp (int): {given_unix_timestamp}")
        print(f"Given datetime: {datetime.datetime.fromtimestamp(given_unix_timestamp)}")

        # 3. Compare the two values
        if file_mtime_unix > given_unix_timestamp:
            return 1
        elif file_mtime_unix < given_unix_timestamp:
            return -1
        else:
            return 0
    except FileNotFoundError:
        return -2
    except ValueError:
        return -2
    except Exception as e:
        return -2
    
def generate_netplan_yaml(interfaces):
    """
    Generates a Netplan YAML configuration from a given JSON input.

    Args:
        interfaces (dict): A dictionary containing interface configurations.

    Returns:
        str: A string representing the Netplan YAML configuration.
    """

    netplan_config = {
        "network": {
            "version": 2,
            "renderer": "networkd",
            "ethernets": {}
        }
    }

    for interface_name, interface_config in interfaces.items():
        ethernet_config = {
            "dhcp4": False,  # Assuming static configuration for provided data
            "addresses": [],
            "routes": []
        }
        mac_address = interface_config.get("mac")
        if mac_address:
            ethernet_config["macaddress"] = mac_address

        # IP address and netmask
        ipv4_address = interface_config.get("ipv4")
        netmask = interface_config.get("netmask")
        if ipv4_address and netmask:
            # Convert netmask to CIDR prefix
            from ipaddress import IPv4Interface
            try:
                ip_interface = IPv4Interface((ipv4_address, netmask))
                ethernet_config["addresses"].append(str(ip_interface))
            except Exception as e:
                print(f"Warning: Could not parse IP address or netmask for {interface_name}: {e}")

        # Gateway
        gateway = interface_config.get("gateway")
        if gateway:
            ethernet_config["routes"].append({"to": "0.0.0.0/0", "via": gateway})

        # Routes
        routes = interface_config.get("routes", [])
        for route in routes:
            to = route.get("ip_or_range")
            is_default = route.get("default", False)

            if to:
                if is_default:
                    # Default route is already handled by 'gateway'
                    continue
                else:
                    route_entry = {"to": to}
                    ethernet_config["routes"].append(route_entry)

        netplan_config["network"]["ethernets"][interface_name] = ethernet_config

    # Use a custom representer to prevent aliases for repeated data (e.g., gateway)
    class NoAliasDumper(yaml.Dumper):
        def ignore_aliases(self, data):
            return True

    return yaml.dump(netplan_config, Dumper=NoAliasDumper, sort_keys=False, indent=4)

def remove_netplan_files(netplan_dir="/sysroot/etc/netplan/"):
    """
    Removes all .yaml files from the specified Netplan directory.

    Args:
        netplan_dir (str): The directory where Netplan YAML files are located.
                           Defaults to "/etc/netplan/".
    """
    print(f"Attempting to remove Netplan YAML files from: {netplan_dir}")
    if not os.path.isdir(netplan_dir):
        print(f"Error: Directory '{netplan_dir}' does not exist.")
        return

    for filename in os.listdir(netplan_dir):
        if filename.endswith(".yaml"):
            file_path = os.path.join(netplan_dir, filename)
            try:
                os.remove(file_path)
                print(f"Successfully removed: {file_path}")
            except OSError as e:
                print(f"Error removing {file_path}: {e}")

def generate_ifupdown_interfaces(interfaces):
    """
    Generates ifupdown configuration content for files under /etc/network/interfaces.d/

    Args:
        interfaces (dict): A dictionary containing interface configurations.

    Returns:
        dict: A dictionary where keys are interface names and values are
              the string content for each interface's configuration file.
    """
    ifupdown_configs = {}

    for interface_name, interface_config in interfaces.items():
        config_lines = [
            f"auto {interface_name}",
            f"iface {interface_name} inet static"
        ]

        # MAC Address
        mac_address = interface_config.get("macaddress")
        if mac_address:
            config_lines.append(f"    hwaddress ether {mac_address}")

        # IP address and netmask
        ipv4_address = interface_config.get("ipv4")
        netmask = interface_config.get("netmask")
        if ipv4_address and netmask:
            try:
                from ipaddress import IPv4Interface
                # Use IPv4Interface to ensure valid IP and derive netmask (though we have it)
                # It's more for validation here, the values are used directly.
                IPv4Interface((ipv4_address, netmask)) # Just to validate format
                config_lines.append(f"    address {ipv4_address}")
                config_lines.append(f"    netmask {netmask}")
            except Exception as e:
                print(f"Warning (ifupdown): Could not parse IP address or netmask for {interface_name}: {e}")

        # Gateway
        gateway = interface_config.get("gateway")
        if gateway:
            config_lines.append(f"    gateway {gateway}")

        # Static Routes
        routes = interface_config.get("routes", [])
        for route in routes:
            to = route.get("ip_or_range")
            is_default = route.get("default", False)

            if to:
                if not is_default:
                    # ifupdown uses 'post-up' for static routes
                    # Ensure route destination is valid
                    try:
                        # Validate the route destination (e.g., "192.168.0.0/24")
                        config_lines.append(f"    post-up ip route add {to} dev {interface_name}")
                    except Exception as e:
                        print(f"Warning (ifupdown): Could not parse route destination '{to}' for {interface_name}: {e}")


        ifupdown_configs[interface_name] = "\n".join(config_lines) + "\n"

    return ifupdown_configs
    
def save_config_files(config_type, configs, output_dir):
    """
    Saves generated configuration content to files in the specified directory.

    Args:
        config_type (str): "netplan" or "ifupdown".
        configs (str or dict): The configuration content (string for netplan, dict for ifupdown).
        output_dir (str): The directory where files should be saved.
    """
    if not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir)
            print(f"Created directory: {output_dir}")
        except OSError as e:
            print(f"Error creating directory {output_dir}: {e}. Please ensure you have permissions.")
            return

    if config_type == "netplan":
        file_path = os.path.join(output_dir, "01-netcfg.yaml")
        try:
            with open(file_path, "w") as f:
                f.write(configs)
            print(f"Netplan configuration saved to: {file_path}")
        except IOError as e:
            print(f"Error writing Netplan file {file_path}: {e}. Please check permissions.")
    elif config_type == "ifupdown":
        for interface_name, content in configs.items():
            file_path = os.path.join(output_dir, f"{interface_name}.cfg")
            try:
                with open(file_path, "w") as f:
                    f.write(content)
                print(f"ifupdown configuration for {interface_name} saved to: {file_path}")
            except IOError as e:
                print(f"Error writing ifupdown file {file_path}: {e}. Please check permissions.")
    else:
        print(f"Unsupported config_type: {config_type}")

def process_systemd_service():
    """
    Fix systemd service in target
    """
    try:
        chroot_script = """
#!/bin/bash
mount --bind /dev  /sysroot/dev
mount --bind /proc /sysroot/proc
mount --bind /sys  /sysroot/sys
"""
        for service in NodeConfig["systemd"]["enable"]:
            chroot_script += f"chroot /bin/bash -c 'systemctl enable {service}' \n"

        for service in NodeConfig["systemd"]["enable"]:
            chroot_script += f"chroot /bin/bash -c 'systemctl disable {service}' \n"
        
        chroot_script += """
umount /sysroot/sys
umount /sysroot/proc
umount /sysroot/dev
"""
        with open("/tmp/systemd_services.sh", "w") as outf:
            outf.write(chroot_script)

        os.chmod("/tmp/systemd_services.sh", 0o755)
        run_command("/tmp/systemd_services.sh")
    except subprocess.CalledProcessError as exc:
        print(f"failed to setup systemd service in target {exc}")
        os.execv("/bin/bash", ["bash"])

# --- Main Script Execution Flow ---
def main():
    print("--- Starting Initramfs Script ---")

    try:
        os.makedirs("/sysroot")
    except Exception as exc:
        print(f"WARNING: failed to create /sysroot")

    # Early Initramfs Setup (markers go to initramfs /tmp, not persistent across reboots)
    # This phase ensures basic environment is ready.
    print("--- Phase 1: Early Initramfs Setup ---")
    if not setup_initial_filesystems():
        print("Critical Error: Initial filesystem setup failed. Dropping to emergency shell.", file=os.sys.stderr)

    load_kernel_modules()
    create_dev_nodes()
    configure_network()

    global NodeConfig
    NodeConfig = read_node_configuration()
    if not NodeConfig:
        print("Failed to read node configuration from nodeconfigserver")
        os.execv("/bin/bash", ["bash"])

    if not read_ignition_file():
        print("Critical Error: failed to read ignition file", file=os.sys.stderr)
        return

    root_fs = get_ignition_root()
    assemble_raid(root_fs)

    # Device exists?
    print(f"Checking if {root_fs.device} exists")
    if not os.path.exists(root_fs.device):
        if not provision_storage():
            os.execv("/bin/bash", ["bash"])

    # Filesystem created?
    print(f"Checking if FS exists on {root_fs.device}")
    if not get_filesystem_type(root_fs.device):
        if not provision_storage():
            os.execv("/bin/bash", ["bash"])

    print(f"Mounting {root_fs.device} at {SYSROOT} as {root_fs.format}")
    run_command(f"mount -t {root_fs.format} {root_fs.device} {SYSROOT}")

    # If rootfs was not copied properly
    if not os.path.exists(os.path.join("/sysroot/", FS_INSTALLED_MARKER)):
        if not transfer_rootfs():
            os.execv("/bin/bash", ["bash"])
    
    if NodeConfig["provisioning_status"] == "sync":
        transfer_rootfs()
    
    if not os.path.exists(os.path.join("/sysroot/", BOOTSTRAPPED_MARKER)):
        # Create boostrapped file
        generate_bootstrapped_file()
    
    compare_ret = compare_file_mtime_with_unix_timestamp(
        os.path.join("/sysroot/", BOOTSTRAPPED_MARKER), 
        NodeConfig["config_timestamp"])
    if compare_ret == -2:
        print("failed to compare config timestamp")
    
    if compare_ret == -1:
        print("config_timestamp is more recent, regenerating bootstrapped file")
        generate_bootstrapped_file()

    try:
        filename = os.path.join("/sysroot", "etc/hostname")
        print(f"Updating {filename}")
        with open(filename, "w") as outf:
            outf.write(NodeConfig["name"])

        filename = os.path.join("/sysroot", "etc/resolv.conf")
        print(f"Updating {filename}")
        with open(filename, "w") as outf:
            for ds in NodeConfig["dns_servers"]:
                outf.write(f"nameserver {ds}\n")

        os.makedirs("/sysroot/root/.ssh/", exist_ok=True)
        filename = os.path.join("/sysroot", "root/.ssh/authorized_keys")
        print(f"Updating {filename}")
        with open(filename, "w") as outf:
            ssh_key = NodeConfig["ssh_key"]
            outf.write(f"nameserver {ssh_key}\n")

        os.chmod(filename, 0o600)
    except Exception as exc:
        print(f"Failed to personalize the image {exc}")
        os.execv("/bin/bash", ["bash"])

    try:
        remove_netplan_files()

        # Generate netplan
        if NodeConfig["os_type"] == "dgx":
            ifdata = generate_ifupdown_interfaces(NodeConfig["interfaces"])
            save_config_files("ifupdown", ifdata, "/sysroot/etc/network/interfaces.d")
            print(f"Wrote ifupdown {ifdata}")
        else:
            netplan_data = generate_netplan_yaml(NodeConfig["interfaces"])
            save_config_files("netplan", netplan_data, "/sysroot/etc/netplan")        
            print(f"Wrote netplan {netplan_data}")
    except Exception as exc:
        print(f"Failed to generate netplan")
        os.execv("/bin/bash", ["bash"])

    print("Copying kernel and initrd to tmp...")
    shutil.copyfile(os.path.join("/sysroot", NodeConfig["kernel"]), "/tmp/vmlinuz")
    shutil.copyfile(os.path.join("/sysroot", NodeConfig["initrd"]), "/tmp/initrd.img")
    shutil.copyfile(os.path.join("/sysroot", BOOTSTRAPPED_MARKER), "/tmp/kexec.sh")
    os.chmod("/tmp/kexec.sh", 0o700)
    
    process_systemd_service()

    # Flush the changes
    try:
        run_command("sync")
        run_command("umount /sysroot")
    except subprocess.CalledProcessError as exc:
        print(f"Failed to sync sysroot {exc}")

    #Final boot action
    print("\n--- Initramfs Script Complete. Handing over control. ---")
    try:
        os.execv("/tmp/kexec.sh", ["/tmp/kexec.sh"])
    except OSError as e:
        print(f"Error executing /tmp/kexec.sh: {e}", file=os.sys.stderr)

    print("No kexec command was executed or it failed. Dropping to emergency shell.", file=os.sys.stderr)
    os.execv("/bin/bash", ["bash"]) # Fallback to an emergency shell if all else fails

if __name__ == "__main__":
    main()
    os.execv("/bin/bash", ["bash"])