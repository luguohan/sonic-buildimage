# Additional targets used by scripts/bazel/driver.py inside sonic-slave.
# Load after slave.mk: make -f slave.mk -f bazel/native.mk bazel-inventory

BAZEL_IMAGE ?= sonic-vs.bin
BAZEL_INVENTORY ?= target/bazel/native-inventory.json

ifneq ($(strip $(SONIC_BAZEL_REQUESTED_STAGE)),)
.PHONY: $(TARGET_PATH)/$(BAZEL_IMAGE)
endif

# Include the configured Docker ancestors and ordering dependencies.  The
# expansion is deliberately based on the evaluated Make graph, so feature
# switches cannot leave a consumer of the SWSS layer outside the Bazel graph.
bazel_docker_closure = $(sort $(1) $(foreach image,$(1),$(call bazel_docker_closure,$($(image)_LOAD_DOCKERS) $($(image)_AFTER))))
bazel_uses_swss = $(strip $(if $(filter $(SWSS) $(SWSS_DBG),$($(1)_DEPENDS) $($(1)_DBG_DEPENDS)),y,$(foreach image,$($(1)_LOAD_DOCKERS) $($(1)_AFTER),$(call bazel_uses_swss,$(image)))))
BAZEL_SELECTED_DOCKERS := $(call bazel_docker_closure,$($(BAZEL_IMAGE)_DOCKERS) $(SONIC_PACKAGES_LOCAL))
BAZEL_OWNED_DOCKERS := $(sort $(foreach image,$(BAZEL_SELECTED_DOCKERS),$(if $(call bazel_uses_swss,$(image)),$(image))))
bazel_env_name = $(subst .,_,$(subst -,_,$(1)))

bazel-inventory: export BAZEL_IMAGE := $(BAZEL_IMAGE)
bazel-inventory: export BAZEL_PLATFORM := $(CONFIGURED_PLATFORM)
bazel-inventory: export BAZEL_ARCH := $(CONFIGURED_ARCH)
bazel-inventory: export BAZEL_DISTRO := $(BLDENV)
bazel-inventory: export BAZEL_SWSS := $(SWSS)
bazel-inventory: export BAZEL_SWSS_DBG := $(SWSS_DBG)
bazel-inventory: export BAZEL_SWSS_DEPENDS := $($(SWSS)_DEPENDS)
bazel-inventory: export BAZEL_SWSS_RDEPENDS := $(call expand,$($(SWSS)_RDEPENDS),RDEPENDS)
bazel-inventory: export BAZEL_SWSS_DEB_BUILD_OPTIONS := $(strip $(DEB_BUILD_OPTIONS) $($(SWSS)_DEB_BUILD_OPTIONS))
bazel-inventory: export BAZEL_SWSS_DEB_BUILD_PROFILES := $(strip $($(SWSS)_DEB_BUILD_PROFILES))
bazel-inventory: export BAZEL_SELECTED_DOCKERS := $(BAZEL_SELECTED_DOCKERS)
bazel-inventory: export BAZEL_INSTALLED_DOCKERS := $($(BAZEL_IMAGE)_DOCKERS)
bazel-inventory: export BAZEL_OWNED_DOCKERS := $(BAZEL_OWNED_DOCKERS)
bazel-inventory: export BAZEL_LOCAL_PACKAGES := $(SONIC_PACKAGES_LOCAL)
bazel-inventory: export BAZEL_REMOTE_PACKAGES := $(SONIC_PACKAGES)
bazel-inventory: export BAZEL_RFS_DEPENDS := $($(BAZEL_IMAGE)_RFS_DEPENDS)
bazel-inventory: export BAZEL_IMAGE_FILES := $($(BAZEL_IMAGE)_FILES)
bazel-inventory: export BAZEL_IMAGE_INSTALLS := $($(BAZEL_IMAGE)_INSTALLS) $($(BAZEL_IMAGE)_LAZY_INSTALLS) $($(BAZEL_IMAGE)_LAZY_BUILD_INSTALLS)
bazel-inventory: export BAZEL_IMAGE_VERSION := $(SONIC_IMAGE_VERSION)
bazel-inventory: export BAZEL_BUILD_TIMESTAMP := $(BUILD_TIMESTAMP)
bazel-inventory: export BAZEL_BUILD_NUMBER := $(BUILD_NUMBER)
bazel-inventory: export BAZEL_ENABLE_SBOM := $(ENABLE_SBOM)
bazel-inventory: export BAZEL_ENABLE_ASAN := $(ENABLE_ASAN)
bazel-inventory: export BAZEL_INSTALL_DEBUG_TOOLS := $(INSTALL_DEBUG_TOOLS)
bazel-inventory: export BAZEL_BUILD_MULTIASIC_KVM := $(BUILD_MULTIASIC_KVM)
bazel-inventory: export BAZEL_MULTIARCH_QEMU_ENVIRON := $(MULTIARCH_QEMU_ENVIRON)
bazel-inventory: export BAZEL_CROSS_BUILD_ENVIRON := $(CROSS_BUILD_ENVIRON)
bazel-inventory: export BAZEL_POST_BUILD_HOOK := $($(BAZEL_IMAGE)_POST_BUILD_HOOK)
bazel-inventory: export BAZEL_IMAGE_SIGNATURE := $(SONIC_ENABLE_IMAGE_SIGNATURE)
bazel-inventory: export BAZEL_SECURE_UPGRADE_MODE := $(SECURE_UPGRADE_MODE)
bazel-inventory: export BAZEL_CONFIG_FLAGS := INCLUDE_FIPS=$(INCLUDE_FIPS) ENABLE_FIPS=$(ENABLE_FIPS) INCLUDE_P4RT=$(INCLUDE_P4RT) INCLUDE_DASH_HA=$(INCLUDE_DASH_HA) INCLUDE_KUBERNETES=$(INCLUDE_KUBERNETES) INCLUDE_KUBERNETES_MASTER=$(INCLUDE_KUBERNETES_MASTER) BUILD_REDUCE_IMAGE_SIZE=$(BUILD_REDUCE_IMAGE_SIZE) ENABLE_ORGANIZATION_EXTENSIONS=$(ENABLE_ORGANIZATION_EXTENSIONS)

$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_PATH := $($(image)_PATH)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_DEPENDS := $(call expand,$($(image)_DEPENDS),RDEPENDS)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_LOAD_DOCKERS := $($(image)_LOAD_DOCKERS)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_AFTER := $($(image)_AFTER)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_FILES := $($(image)_FILES)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_WHEELS := $(call expand,$($(image)_PYTHON_WHEELS))))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_PYTHON_DEBS := $(call expand,$($(image)_PYTHON_DEBS))))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_INSTALL_DEBS := $($(image)_INSTALL_DEBS)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_INSTALL_WHEELS := $($(image)_INSTALL_PYTHON_WHEELS)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_DEBS_PATH := $($(image)_DEBS_PATH)))
$(foreach image,$(BAZEL_SELECTED_DOCKERS),$(eval bazel-inventory: export BAZEL_DOCKER_$(call bazel_env_name,$(image))_FILES_PATH := $($(image)_FILES_PATH)))

.PHONY: bazel-inventory
bazel-inventory:
	python3 scripts/bazel/make_inventory.py --output "$(BAZEL_INVENTORY)"
