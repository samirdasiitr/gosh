qemu-system-x86_64 -smp 4 -enable-kvm -m 4096 -kernel ../../../tftpboot/goldimage/vmlinuz -initrd initrd.img \
	-append "root=/dev/sda1 rw console=ttyS0 nodeconfigserver=10.10.6.5 nodeconfigserverport=8082" \
       	-nographic \
	-drive file=testdisk.qcow2,format=qcow2,if=virtio \
	-drive file=testdisk-vdb.qcow2,format=qcow2,if=virtio \
	-netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
	-netdev user,id=net1 -device virtio-net-pci,netdev=net1 \
	-vnc 0.0.0.0:10
