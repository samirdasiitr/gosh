#!/usr/bin/env bash

# --- Configuration ---
INITRD_DIR="initrd_build"
OUTPUT_FILE="initrd.img"

SOURCE_MODULES="/vmhost/workspace/tools/simplyboot/build/initrd/extracted/lib/modules"
IGNITION_PATH="/vmhost/workspace/tools/simplyboot/go_src/ignition/bin/amd64/"
IGNITION_CONFIG_PATH="/vmhost/workspace/tools/simplyboot/build/buildrootfs/ignition_raided.json"
PYTHON_BIN="/usr/bin/python3"
PYTHON_LIB="/usr/lib/python3.10"
PYTHON_DIST_PACKAGES="/usr/lib/python3"
PYTHON_VERSION="3.10"

# List of essential binaries to copy from your host system
BINARIES_TO_COPY=() # Initialize an empty array

# Read the file line by line, resolve full path using 'which'
# 'IFS=' prevents leading/trailing whitespace trimming.
# '-r' prevents backslash escapes from being interpreted.
while IFS= read -r line; do
    # Use 'which' to find the full path of the command
    full_path=$(which "$line" 2>/dev/null)
    if [ -n "$full_path" ]; then
        BINARIES_TO_COPY+=("$full_path") # Add the full path to the array
    else
        echo "WARNING: Command '$line' not found in PATH. Skipping."
    fi
done < "./packages"

# --- Functions ---

# Function to display error messages and exit
error_exit() {
    echo "ERROR: $1" >&2
    cleanup
    exit 1
}

# Function to clean up temporary directory
cleanup() {
    echo "Cleaning up temporary directory: $INITRD_DIR"
    #sudo rm -rf "$INITRD_DIR"
}

#
# this helper will takes resolv libs which are
# not directly linked to binaries but needed for dns resolution
# and other names resolving
#
resolv_libs() {
    paths=$(grep -hr ^/ /etc/ld.so.conf*)
    for path in $paths; do
        if [ ! -e "$path/libresolv.so.2" ]; then
            continue
        fi
        mkdir -p ./$path
        cp -afL $path/libresolv* ./$path
        cp -fa $path/libnss_{compat,dns,files}* ./$path
        cp -fa $path/libnsl* ./$path
        return
    done

    echo "[-] warning: no libs found for resolving names"
    echo "[-] you probably won't be able to do dns request"
}

copy_binary_with_deps() {
    sourcebin=$(which $1)

    echo "[+] checking ${sourcebin} dependencies"

    if [ ! -e lib64 ]; then mkdir lib64; fi
    if [ ! -e lib ]; then ln -s lib64 lib; fi

    bindirname=$(dirname $sourcebin)
    mkdir -p ./$bindirname
    cp -f $sourcebin ./$sourcebin

    # Copiyng ld-dependancy
    ld=$(ldd $sourcebin | grep ld-linux | awk '{ print $1 }')
    if [ "x$ld" == "x" ];
    then
        echo "$sourcebin not dynamic executable"
        return
    fi

    cp -faL $ld lib/

    # Copying resolv libraries
    resolv_libs

    libs=$(ldd $sourcebin 2>&1 | grep '=>' | grep 'not found' | awk '{ print $1 }' || true)
    for lib in $libs; do
        echo "[-] warning: $lib: not found"
    done

    # Looking for dynamic libraries shared
    libs=$(ldd $sourcebin 2>&1 | grep '=>' | grep -v '=>  (' | awk '{ print $3 }' || true)

    # Checking each libraries
    for lib in $libs; do
        libname=$(basename $lib)

        # Library found and not the already installed one
        if [ -e lib/$libname ] || [ "$lib" == "${PWD}/lib/$libname" ]; then
            continue
        fi

        if [ "$libname" == "not" ]; then
            continue
        fi

        # Grabbing library from host
        #echo "[+] copying $lib"
        libdirname=$(dirname $lib)
        mkdir -p ./$libdirname
        cp -faL $lib ./$lib
    done
}

# --- Main Script ---

echo "Starting initrd creation process..."

# 1. Setup: Create temporary directory for initrd contents
echo "Creating initrd build directory: $INITRD_DIR"
sudo rm -rf "$INITRD_DIR" # Ensure clean slate
mkdir "$INITRD_DIR" || error_exit "Failed to create $INITRD_DIR"
cd "$INITRD_DIR" || error_exit "Failed to change directory to $INITRD_DIR"

# 2. Essential Directories
echo "Creating essential directories inside $INITRD_DIR"
sudo mkdir -p bin dev etc lib lib64 proc sbin sys tmp root var usr || error_exit "Failed to create essential directories"

# Copy loader
cp -f /lib64/ld-linux-x86-64.so.2 ./lib64/ld-linux-x86-64.so.2

# 3. Copy busybox if available (optional, but highly recommended for minimal initrds)
if command -v busybox &> /dev/null; then
    echo "BusyBox found. Copying it to /bin."
    copy_binary_with_deps "$(command -v busybox)" "bin"
    # Create symlinks for busybox utilities if busybox is copied
    echo "Creating busybox symlinks..."
    for cmd in $(busybox --list); do
        if [ "$cmd" == "busybox" ]; then
          continue
        fi
        # Check if the command already exists in bin or sbin to avoid overwriting
        if [ ! -e "bin/$cmd" ] && [ ! -e "sbin/$cmd" ]; then
            # Determine if the original command exists in /bin or /sbin to mimic common locations
            if [ -f "/bin/$cmd" ]; then
                sudo ln -sf /bin/busybox bin/"$cmd"
            elif [ -f "/sbin/$cmd" ]; then
                sudo ln -sf /bin/busybox sbin/"$cmd"
            else
                # Default to /bin if not found in common /bin or /sbin
                sudo ln -sf /bin/busybox bin/"$cmd"
            fi
        fi
    done
else
    echo "BusyBox not found. Consider installing it for a more robust minimal initrd."
fi

# 4. Copy Python 3 and its core components
echo "Copying Python 3 and its standard library..."
# Copy the python3 executable and its immediate dependencies
copy_binary_with_deps "$PYTHON_BIN" "usr/bin" # Place python3 in /usr/bin inside initrd

# Copy the Python standard library
# This is crucial. We need to copy the *contents* of the standard library directory
# into the equivalent path inside the initrd, typically under /usr/lib/pythonX.Y/
# We use 'rsync -a' for efficient recursive copy and preserving symlinks/permissions.
local_python_lib_target="./usr/lib/python${PYTHON_VERSION}"
mkdir -p "$local_python_lib_target" || error_exit "Failed to create Python lib target directory"

# First, copy the standard library without site-packages to ensure core modules
# are available even if site-packages are separate.
echo "[+] Copying Python standard library from $PYTHON_LIB_PATH to $local_python_lib_target"
rsync -a "$PYTHON_LIB/" "$local_python_lib_target/" || error_exit "Failed to copy Python standard library"

echo "[+] Copying Python dist-packages from $PYTHON_DIST_PACKAGES to ./usr/lib/python3/dist-packages/"
mkdir -p "./usr/lib/python3/dist-packages/"
rsync -a "$PYTHON_DIST_PACKAGES/" "./usr/lib/python3/" || error_exit "Failed to copy Python site-packages"

echo "[+] Copying Python dist-packages from /usr/local/lib/python${PYTHON_VERSION}/dist-packages"
mkdir -p ./usr/local/lib/python${PYTHON_VERSION}/dist-packages/
rsync -a /usr/local/lib/python${PYTHON_VERSION}/dist-packages ./usr/local/lib/python${PYTHON_VERSION}/

# 5. Copy Tools and 4. Identify/Copy Dependencies
echo "Copying binaries and their dependencies..."
for binary in "${BINARIES_TO_COPY[@]}"; do
    # Determine destination directory (bin or sbin) based on the resolved full path
    if [[ "$binary" == "/bin/"* ]]; then
        copy_binary_with_deps "$binary" "bin"
    elif [[ "$binary" == "/sbin/"* ]]; then
        copy_binary_with_deps "$binary" "sbin"
    elif [[ "$binary" == "/usr/bin/"* ]]; then
        copy_binary_with_deps "$binary" "bin" # Often /usr/bin is symlinked to /bin on modern systems
    elif [[ "$binary" == "/usr/sbin/"* ]]; then
        copy_binary_with_deps "$binary" "sbin" # Often /usr/sbin is symlinked to /sbin on modern systems
    else
        echo "WARNING: Unknown binary path: $binary. Copying to /bin."
        copy_binary_with_deps "$binary" "bin"
    fi
done

# 6. Create Device Nodes
echo "Creating device nodes..."
sudo mknod dev/console c 5 1 || error_exit "Failed to create dev/console"
sudo mknod dev/null c 1 3 || error_exit "Failed to create dev/null"
sudo mknod dev/zero c 1 5 || error_exit "Failed to create dev/zero"
sudo mknod dev/tty c 5 0 || error_exit "Failed to create dev/tty"
sudo mknod dev/urandom c 1 9 || error_exit "Failed to create dev/tty"

# 7. Basic Configuration (Optional)
echo "Creating basic /etc/fstab and /etc/inittab (optional)"
sudo mkdir -p etc
echo "# /etc/fstab: static file system information." | sudo tee etc/fstab
echo "proc /proc proc defaults 0 0" | sudo tee -a etc/fstab
echo "sysfs /sys sysfs defaults 0 0" | sudo tee -a etc/fstab

cp /sbin/dhclient-script ./sbin/dhclient-script

# 8. Init Script
echo "Creating /init script..."
# This is the script that the kernel will execute as PID 1
cp ../init ./init
chmod +x ./init

sudo chmod +x init || error_exit "Failed to set executable permission on /init"

# 9. Copy modules
echo "Copying kernel modules"
cp -fr $SOURCE_MODULES ./lib/

# 10. Copy modules
echo "Copying ignition to target"
cp $IGNITION_PATH/* ./usr/bin/
cp $IGNITION_CONFIG_PATH ./ignition.json

# 11. Permissions
echo "Setting permissions..."
sudo chmod 755 -R bin/ sbin/ || error_exit "Failed to set permissions on binaries"
sudo chmod 755 -R lib/ lib64/ || error_exit "Failed to set permissions on libraries" # Libraries need to be readable
sudo chmod 755 -R dev || error_exit "Failed to set permissions on dev"

# 12. Copy init.py
cp ../init.py ./init.py

# 13. Package: Create the initrd.img archive
echo "Creating cpio archive: $OUTPUT_FILE"
# find . -print0 | cpio --null -ov --format=newc > "../$OUTPUT_FILE"
# Use -H newc for compatibility
sudo find . | sudo cpio -o -H newc > "../$OUTPUT_FILE" || error_exit "Failed to create cpio archive"

# Go back to original directory before cleanup
cd .. || error_exit "Failed to change back to original directory"

# 10. Cleanup
cleanup

echo "Initrd creation complete. Your initrd image is: $OUTPUT_FILE"
echo "You can now test this with QEMU or a virtual machine, e.g.:"
echo "qemu-system-x86_64 -kernel /boot/vmlinuz-$(uname -r) -initrd $OUTPUT_FILE -append \"root=/dev/sda1 rw console=ttyS0\" -nographic"
echo "(Replace /boot/vmlinuz-... and /dev/sda1 with your actual kernel and root device)"