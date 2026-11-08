cmake_minimum_required(VERSION 3.16)

project(citybrain
        VERSION 1.1.0
        DESCRIPTION "City Brain deterministic/mathematical decision core"
        LANGUAGES C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(CITYBRAIN_BUILD_TESTS "Build the C test suite" ON)
option(CITYBRAIN_BUILD_BENCH "Build the benchmark driver" ON)
option(CITYBRAIN_WERROR      "Treat compiler warnings as errors" ON)

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

set(CITYBRAIN_SOURCES
    src/common.c
    src/fusion.c
    src/detector.c
    src/graph.c
    src/decision.c
    src/assignment.c
    src/cache.c
    src/calibration.c
    src/mdp.c
    src/example_profile.c
    src/physics.c)

set(CITYBRAIN_HEADERS
    include/citybrain/citybrain.h
    include/citybrain/common.h
    include/citybrain/fusion.h
    include/citybrain/detector.h
    include/citybrain/graph.h
    include/citybrain/decision.h
    include/citybrain/assignment.h
    include/citybrain/cache.h
    include/citybrain/calibration.h
    include/citybrain/mdp.h
    include/citybrain/example_profile.h
    include/citybrain/physics.h)

# Strict warnings, since input comes from outside the process.
if(MSVC)
  set(CITYBRAIN_WARNINGS /W4)
  if(CITYBRAIN_WERROR)
    list(APPEND CITYBRAIN_WARNINGS /WX)
  endif()
else()
  set(CITYBRAIN_WARNINGS
      -Wall -Wextra -Wpedantic
      -Wshadow -Wstrict-prototypes -Wmissing-prototypes -Wold-style-definition
      -Wvla -Wdouble-promotion -Wcast-qual -Wpointer-arith
      -Wconversion -Wsign-conversion
      -Wformat=2 -Wundef -Wwrite-strings)
  if(CITYBRAIN_WERROR)
    list(APPEND CITYBRAIN_WARNINGS -Werror)
  endif()
endif()

# Compile once, link into both the shared and static library.
add_library(citybrain_objects OBJECT ${CITYBRAIN_SOURCES})
target_include_directories(citybrain_objects PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_compile_options(citybrain_objects PRIVATE ${CITYBRAIN_WARNINGS})
set_target_properties(citybrain_objects PROPERTIES POSITION_INDEPENDENT_CODE ON)

add_library(citybrain SHARED $<TARGET_OBJECTS:citybrain_objects>)
add_library(citybrain_static STATIC $<TARGET_OBJECTS:citybrain_objects>)

foreach(tgt citybrain citybrain_static)
  target_include_directories(${tgt} PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
  if(NOT MSVC)
    target_link_libraries(${tgt} PUBLIC m)
  endif()
endforeach()

set_target_properties(citybrain PROPERTIES
    VERSION ${PROJECT_VERSION}
    SOVERSION ${PROJECT_VERSION_MAJOR}
    OUTPUT_NAME citybrain
    C_VISIBILITY_PRESET default)
set_target_properties(citybrain_static PROPERTIES
    OUTPUT_NAME citybrain)
target_compile_definitions(citybrain PRIVATE CITYBRAIN_BUILD_SHARED)

add_library(citybrain::citybrain ALIAS citybrain)
add_library(citybrain::citybrain_static ALIAS citybrain_static)

# Tests and benchmarks live in ../tests, a sibling of this directory.
if(CITYBRAIN_BUILD_TESTS)
  enable_testing()
  add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../tests ${CMAKE_CURRENT_BINARY_DIR}/tests)
endif()

if(CITYBRAIN_BUILD_BENCH)
  add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../tests/bench ${CMAKE_CURRENT_BINARY_DIR}/bench)
endif()

# install
install(TARGETS citybrain citybrain_static
        EXPORT citybrainTargets
        LIBRARY  DESTINATION ${CMAKE_INSTALL_LIBDIR}
        ARCHIVE  DESTINATION ${CMAKE_INSTALL_LIBDIR}
        RUNTIME  DESTINATION ${CMAKE_INSTALL_BINDIR}
        INCLUDES DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})

install(FILES ${CITYBRAIN_HEADERS}
        DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/citybrain)

install(EXPORT citybrainTargets
        FILE citybrainTargets.cmake
        NAMESPACE citybrain::
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/citybrain)

configure_package_config_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/cmake/citybrainConfig.cmake.in
    ${CMAKE_CURRENT_BINARY_DIR}/citybrainConfig.cmake
    INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/citybrain)

write_basic_package_version_file(
    ${CMAKE_CURRENT_BINARY_DIR}/citybrainConfigVersion.cmake
    VERSION ${PROJECT_VERSION}
    COMPATIBILITY SameMajorVersion)

install(FILES
    ${CMAKE_CURRENT_BINARY_DIR}/citybrainConfig.cmake
    ${CMAKE_CURRENT_BINARY_DIR}/citybrainConfigVersion.cmake
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/citybrain)

# pkg-config fallback for non-CMake builds.
configure_file(${CMAKE_CURRENT_SOURCE_DIR}/cmake/citybrain.pc.in
               ${CMAKE_CURRENT_BINARY_DIR}/citybrain.pc @ONLY)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/citybrain.pc
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/pkgconfig)
