.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

MAIN_TARGET = monit_$(MONIT_VERSION)_$(CONFIGURED_ARCH).deb
DERIVED_TARGETS = monit-dbgsym_$(MONIT_VERSION)_$(CONFIGURED_ARCH).deb

MONIT_SOURCE_URL ?=
MONIT_SOURCE_DSC_SHA256 ?=

ifneq ($(strip $(MONIT_SOURCE_URL)),)
ifeq ($(strip $(MONIT_SOURCE_DSC_SHA256)),)
$(error MONIT_SOURCE_URL and MONIT_SOURCE_DSC_SHA256 must be set together)
endif
else
ifneq ($(strip $(MONIT_SOURCE_DSC_SHA256)),)
$(error MONIT_SOURCE_URL and MONIT_SOURCE_DSC_SHA256 must be set together)
endif
endif

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% :
	# Remove any stale files
	rm -rf ./monit

ifeq ($(strip $(MONIT_SOURCE_URL)),)
	# Clone monit repo
	git clone https://salsa.debian.org/debian/monit.git
	pushd ./monit

	# Reset HEAD to the commit of the proper tag
	# NOTE: Using "git checkout <tag_name>" here detaches our HEAD,
	# which stg doesn't like, so we use this method instead
	# NOTE: For some reason, tags in the Debian monit repo are prefixed with "1%"
	git reset --hard debian/1\%$(MONIT_VERSION)
else
	# The caller supplies an authenticated .dsc SHA-256. Verify it before
	# extracting the archives whose strong checksums it records.
	dget -u -d "$(MONIT_SOURCE_URL)/monit_$(MONIT_VERSION).dsc"
	printf '%s  %s\n' "$(MONIT_SOURCE_DSC_SHA256)" "monit_$(MONIT_VERSION).dsc" | sha256sum -c -
	dpkg-source --require-strong-checksums -x "monit_$(MONIT_VERSION).dsc" monit
	test "$$(dpkg-parsechangelog -l monit/debian/changelog -S Version)" = "1:$(MONIT_VERSION)"
	pushd ./monit
	git init
	git add -f .
	git commit -m "unmodified monit source"
endif

	# Apply patches
	stg init
	stg import -s ../patch/series

ifeq ($(CROSS_BUILD_ENVIRON), y)
	patch -p1 < ../patch/cross-compile-changes.patch
endif

	# Build source and Debian packages
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -rfakeroot -b -us -uc --host-arch $(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -rfakeroot -b -us -uc -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd

	# Move the newly-built .deb packages to the destination directory
	mv $(DERIVED_TARGETS) $* $(DEST)/

$(addprefix $(DEST)/, $(DERIVED_TARGETS)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
