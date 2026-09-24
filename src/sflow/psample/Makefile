.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

MAIN_TARGET = $(PSAMPLE)
DERIVED_TARGETS = $(PSAMPLE_DBG)
export PSAMPLE_SOURCE_REPOSITORY PSAMPLE_SOURCE_RECORDED_COMMIT PSAMPLE_SOURCE_RECIPE_COMMIT

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% :

	rm -fr ./libpsample
	recipe_revision=e48fad2
	if [[ -n "$${PSAMPLE_SOURCE_REPOSITORY:-}" || -n "$${PSAMPLE_SOURCE_RECORDED_COMMIT:-}" || -n "$${PSAMPLE_SOURCE_RECIPE_COMMIT:-}" ]]; then
		if [[ -z "$${PSAMPLE_SOURCE_REPOSITORY:-}" || -z "$${PSAMPLE_SOURCE_RECORDED_COMMIT:-}" || -z "$${PSAMPLE_SOURCE_RECIPE_COMMIT:-}" ]]; then
			echo "PSAMPLE source repository, recorded commit, and recipe commit must be set together" >&2
			exit 1
		fi
		python3 ../../../scripts/bazel/psample_source.py clone \
			--repository "$${PSAMPLE_SOURCE_REPOSITORY}" --destination ./libpsample \
			--recipe-revision "$${recipe_revision}" --recorded-commit "$${PSAMPLE_SOURCE_RECORDED_COMMIT}" \
			--recipe-commit "$${PSAMPLE_SOURCE_RECIPE_COMMIT}"
	else
		git clone https://github.com/Mellanox/libpsample.git
	fi
	cp -r debian libpsample
	if [[ -n "$${PSAMPLE_SOURCE_REPOSITORY:-}" ]]; then
		python3 ../../../scripts/bazel/psample_source.py checkout --destination ./libpsample \
			--recipe-revision "$${recipe_revision}" --recorded-commit "$${PSAMPLE_SOURCE_RECORDED_COMMIT}" \
			--recipe-commit "$${PSAMPLE_SOURCE_RECIPE_COMMIT}"
	fi

	pushd ./libpsample
	if [[ -z "$${PSAMPLE_SOURCE_REPOSITORY:-}" ]]; then
		git checkout -b libpsample -f "$${recipe_revision}"
	fi

ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -rfakeroot -b -us -uc -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -rfakeroot -b -us -uc -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd

	mv $(DERIVED_TARGETS) $* $(DEST)/

$(addprefix $(DEST)/, $(DERIVED_TARGETS)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
