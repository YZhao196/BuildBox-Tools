cmake_minimum_required(VERSION 3.16)
project(BuildBox VERSION 0.1.0 LANGUAGES CXX)

# Header-only, so there is nothing to compile and nothing to install but headers.
# That is deliberate: a build system on a robot is a hostile place for a compiled
# artefact, and a header has no ABI to break and no binary to match the compiler
# that produced it.
add_library(buildbox INTERFACE)
add_library(buildbox::buildbox ALIAS buildbox)

target_include_directories(
  buildbox
  INTERFACE $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:include>
)
target_compile_features(buildbox INTERFACE cxx_std_17)

if(WIN32)
  # Winsock is a library rather than just headers, and nothing links it for you.
  target_link_libraries(buildbox INTERFACE ws2_32)
endif()

# --- tests ------------------------------------------------------------------
option(BUILDBOX_BUILD_TESTS "Build the client's tests" ON)
if(BUILDBOX_BUILD_TESTS)
  enable_testing()

  add_executable(buildbox_tests tests/test_client.cpp)
  target_link_libraries(buildbox_tests PRIVATE buildbox::buildbox)
  add_test(NAME buildbox_tests COMMAND buildbox_tests)

  # The reading builders, and the coverage check that every catalogue preset has
  # one. This is the test that fails when a preset is added without a function.
  add_executable(buildbox_data_tests tests/test_data.cpp)
  target_link_libraries(buildbox_data_tests PRIVATE buildbox::buildbox)
  add_test(NAME buildbox_data_tests COMMAND buildbox_data_tests)

  set_target_properties(buildbox_tests buildbox_data_tests
                        PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON)
  target_compile_options(buildbox_tests PRIVATE $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra>)
  target_compile_options(buildbox_data_tests PRIVATE $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra>)
endif()

# --- examples ---------------------------------------------------------------
option(BUILDBOX_BUILD_EXAMPLES "Build the examples" ON)
if(BUILDBOX_BUILD_EXAMPLES)
  add_executable(robot_node examples/robot_node.cpp)
  target_link_libraries(robot_node PRIVATE buildbox::buildbox)
endif()

# --- install ------------------------------------------------------------------
include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

install(TARGETS buildbox EXPORT BuildBoxTargets)
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})

install(
  EXPORT BuildBoxTargets
  FILE BuildBoxTargets.cmake
  NAMESPACE buildbox::
  DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/BuildBox
)

configure_package_config_file(
  cmake/BuildBoxConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/BuildBoxConfig.cmake
  INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/BuildBox
)
write_basic_package_version_file(
  ${CMAKE_CURRENT_BINARY_DIR}/BuildBoxConfigVersion.cmake
  VERSION ${PROJECT_VERSION}
  COMPATIBILITY SameMajorVersion
)

install(
  FILES ${CMAKE_CURRENT_BINARY_DIR}/BuildBoxConfig.cmake
        ${CMAKE_CURRENT_BINARY_DIR}/BuildBoxConfigVersion.cmake
  DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/BuildBox
)
