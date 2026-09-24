.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

MAIN_TARGET = $(HSFLOWD)
DERIVED_TARGET = $(HSFLOWD_DBG)
export HSFLOWD_SOURCE_REPOSITORY HSFLOWD_SOURCE_COMMIT

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% :
	rm -fr ./host-sflow
	if [[ -n "$${HSFLOWD_SOURCE_REPOSITORY:-}" || -n "$${HSFLOWD_SOURCE_COMMIT:-}" ]]; then
		if [[ -z "$${HSFLOWD_SOURCE_REPOSITORY:-}" || -z "$${HSFLOWD_SOURCE_COMMIT:-}" ]]; then
			echo "HSFLOWD_SOURCE_REPOSITORY and HSFLOWD_SOURCE_COMMIT must be set together" >&2
			exit 1
		fi
		python3 ../../../scripts/bazel/host_sflow_source.py \
			--repository "$${HSFLOWD_SOURCE_REPOSITORY}" --destination ./host-sflow \
			--tag "v$(HSFLOWD_VERSION)-$(HSFLOWD_SUBVERSION)" --commit "$${HSFLOWD_SOURCE_COMMIT}"
	else
		git clone https://github.com/sflow/host-sflow
	fi

	pushd ./host-sflow
	if [[ -z "$${HSFLOWD_SOURCE_REPOSITORY:-}" ]]; then
		git checkout -b sflow tags/v$(HSFLOWD_VERSION)-$(HSFLOWD_SUBVERSION)
	fi

        # Apply patch series
	stg init
	stg import -s ../patch/series

	mkdir -p debian
	cp -r DEBIAN_build/* debian
	chmod u+x debian/rules
	sed -i -e s/_VERSION_/$(HSFLOWD_VERSION)-$(HSFLOWD_SUBVERSION)/g debian/changelog

ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -rfakeroot -b -us -uc -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -rfakeroot -b -us -uc -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd

	mv $(DERIVED_TARGET) $* $(DEST)/

$(addprefix $(DEST)/, $(DERIVED_TARGET)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
