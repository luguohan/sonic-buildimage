SHELL = /bin/bash
.ONESHELL:
.SHELLFLAGS += -xe

P4LANG_TARGET = p4lang-pi_$(P4LANG_PI_VERSION_FULL)_$(CONFIGURED_ARCH).deb

$(addprefix $(DEST)/, $(P4LANG_TARGET)): $(DEST)/% :
# 	# Remove any stale files
	rm -rf p4lang-pi-$(P4LANG_PI_VERSION)
	dget -u p4lang-pi_$(P4LANG_PI_VERSION_FULL).dsc http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-pi_$(P4LANG_PI_VERSION_FULL).dsc
	pushd p4lang-pi-$(P4LANG_PI_VERSION)
	patch -p1 -i ../p4lang-pi.patch/use-boost-1.83.patch
	patch -p1 -i ../p4lang-pi.patch/remove-protobuf-from-debian-dependency.patch
	patch -p1 -i ../p4lang-pi.patch/remove-init-file.patch
	patch -p1 -i ../p4lang-pi.patch/use-loopback-for-test-clients.patch
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -us -uc -b -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -us -uc -b -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd
	mv $* $(DEST)/

P4LANG_BMV2_TARGET = p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL)_$(CONFIGURED_ARCH).deb

$(addprefix $(DEST)/, $(P4LANG_BMV2_TARGET)): $(DEST)/% :
# 	# Remove any stale files
	rm -rf p4lang-bmv2-$(P4LANG_BMV2_VERSION)
	dget -u p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL).dsc http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL).dsc
	pushd p4lang-bmv2-$(P4LANG_BMV2_VERSION)
	patch -p1 -i ../p4lang-bmv2.patch/use-boost-1.83.patch
	patch -p1 -i ../p4lang-bmv2.patch/remove-unnecessary-dependencies.patch
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -us -uc -b -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -us -uc -b -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd
	mv $* $(DEST)/

P4LANG_P4C_TARGET = p4lang-p4c_$(P4LANG_P4C_VERSION_FULL)_$(CONFIGURED_ARCH).deb
$(addprefix $(DEST)/, $(P4LANG_P4C_TARGET)): $(DEST)/% :
# 	# Remove any stale files
	rm -rf p4lang-p4c-$(P4LANG_P4C_VERSION)
	# Verify the selected Debian 11 source files before extracting them.
	curl -fL -o p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).dsc http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).dsc
	echo "2ee97ffd3f82d67e04ee149f12ef8c58377f9db37e76d4aaa54518178c7f0fb7  p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).dsc" | sha256sum -c -
	patch --batch --forward --reject-file=- -p0 -i p4lang-p4c-regional-source.dsc.patch
	curl -fL -o p4lang-p4c_$(P4LANG_P4C_VERSION).orig.tar.gz https://downloadcontent.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-p4c_$(P4LANG_P4C_VERSION).orig.tar.gz
	curl -fL -o p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).debian.tar.xz http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).debian.tar.xz
	echo "a41d616add63924a098edaab446809f543ffadfc2e40356f9b73986f324c668a  p4lang-p4c_$(P4LANG_P4C_VERSION).orig.tar.gz" | sha256sum -c -
	echo "2a39bf04c8d084724014b71cdeb3b91dbe7a32bf1bd6cd499e5e132aa3e4cb40  p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).debian.tar.xz" | sha256sum -c -
	dpkg-source -x --require-strong-checksums p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).dsc
	pushd p4lang-p4c-$(P4LANG_P4C_VERSION)
	patch -p1 -i ../p4lang-p4c.patch/use-boost-1.83.patch
	patch -p1 -i ../p4lang-p4c.patch/fix-build-for-boost-1.83.patch
	patch -p1 -i ../p4lang-p4c.patch/update-thrift-dependency.patch
	patch -p1 -i ../p4lang-p4c.patch/remove-protobuf-from-debian-dependency.patch
	patch -p1 -i ../p4lang-p4c.patch/remove-scapy-from-debian-dependency.patch
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -us -uc -b -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -us -uc -b -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd
	mv $* $(DEST)/

