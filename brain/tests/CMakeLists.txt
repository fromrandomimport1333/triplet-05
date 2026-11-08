# Each suite is a standalone executable linked against the static library, so
# a failure names one file rather than one giant binary.
set(CITYBRAIN_TEST_SUITES
    test_fusion
    test_detector
    test_graph
    test_decision
    test_assignment
    test_cache
    test_calibration
    test_mdp
    test_adversarial
    test_physics
    test_property
    test_vectors)

foreach(suite ${CITYBRAIN_TEST_SUITES})
  add_executable(${suite} ${suite}.c)
  target_link_libraries(${suite} PRIVATE citybrain_static)
  target_include_directories(${suite} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
  target_compile_options(${suite} PRIVATE ${CITYBRAIN_WARNINGS})
  add_test(NAME ${suite} COMMAND ${suite})
endforeach()

# The golden-vector runner needs the path to the data file.
target_compile_definitions(test_vectors PRIVATE
    CB_VECTORS_PATH="${CMAKE_CURRENT_SOURCE_DIR}/vectors/golden.csv")

# A longer property run, not part of the default suite.
add_test(NAME test_property_long COMMAND test_property 50000)
set_tests_properties(test_property_long PROPERTIES LABELS "long" DISABLED TRUE)
