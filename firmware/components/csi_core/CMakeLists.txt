## IDF 组件形式的 CMakeLists（固件侧）；PC 对拍直接用 zig cc 编译（见 pc_main.c 头部注释）。
idf_component_register(SRCS "src/csi_core.c" INCLUDE_DIRS "include")
