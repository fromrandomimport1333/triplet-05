add_executable(cb_bench bench.c)
target_link_libraries(cb_bench PRIVATE citybrain_static)
target_compile_options(cb_bench PRIVATE ${CITYBRAIN_WARNINGS})
