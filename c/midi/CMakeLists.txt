cmake_minimum_required(VERSION 3.16)
project(arranger_midi C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

option(MIDI_WERROR "Treat compiler warnings as errors" ON)
option(MIDI_SANITIZE "Build with AddressSanitizer and UndefinedBehaviorSanitizer (GCC or Clang)" OFF)

if(MSVC)
  set(MIDI_WARNINGS /W4)
  if(MIDI_WERROR)
    list(APPEND MIDI_WARNINGS /WX)
  endif()
else()
  set(MIDI_WARNINGS -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion -Wcast-qual -Wstrict-prototypes)
  if(MIDI_WERROR)
    list(APPEND MIDI_WARNINGS -Werror)
  endif()
endif()

if(MIDI_SANITIZE)
  if(MSVC)
    add_compile_options(/fsanitize=address)
  else()
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all)
    add_link_options(-fsanitize=address,undefined)
  endif()
endif()

# The parser: one C file, no dependencies.
add_library(midi STATIC src/midi.c)
target_include_directories(midi PUBLIC include)
target_compile_options(midi PRIVATE ${MIDI_WARNINGS})

# The JSON dumper the cross-language diff uses.
add_executable(midi_dump src/midi_dump.c)
target_link_libraries(midi_dump PRIVATE midi)
target_compile_options(midi_dump PRIVATE ${MIDI_WARNINGS})

# Unity (MIT, vendored under tests/unity) and the unit tests.
enable_testing()
add_library(unity STATIC tests/unity/unity.c)
target_include_directories(unity PUBLIC tests/unity)
target_compile_definitions(unity PUBLIC UNITY_INCLUDE_DOUBLE)

add_executable(test_midi tests/test_midi.c)
target_link_libraries(test_midi PRIVATE midi unity)
target_compile_options(test_midi PRIVATE ${MIDI_WARNINGS})
add_test(NAME midi_unit COMMAND test_midi)

# The dumper must refuse junk with the documented exit status.
add_test(NAME midi_dump_rejects_junk COMMAND midi_dump ${CMAKE_CURRENT_SOURCE_DIR}/CMakeLists.txt)
set_tests_properties(midi_dump_rejects_junk PROPERTIES WILL_FAIL TRUE)
add_test(NAME midi_dump_usage COMMAND midi_dump)
set_tests_properties(midi_dump_usage PROPERTIES WILL_FAIL TRUE)
