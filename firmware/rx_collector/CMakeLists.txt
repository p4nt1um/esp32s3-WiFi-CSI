cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")
# WiFi 凭据放在不提交的 sdkconfig.secrets（文件不存在时自动跳过，不影响他人构建）
set(SDKCONFIG_DEFAULTS "sdkconfig.defaults;sdkconfig.secrets")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rx_collector)
