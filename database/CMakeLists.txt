cmake_minimum_required(VERSION 3.16)
project(sonic_master_db_builder CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O3")

find_package(PkgConfig REQUIRED)
pkg_check_modules(SQLITE3 REQUIRED sqlite3)

add_executable(master_db_builder master_db_builder.cpp)
target_include_directories(master_db_builder PRIVATE ${SQLITE3_INCLUDE_DIRS})
target_link_libraries(master_db_builder PRIVATE ${SQLITE3_LIBRARIES})
target_link_directories(master_db_builder PRIVATE ${SQLITE3_LIBRARY_DIRS})

# Build:
#   nix develop        # enter the devshell (provides gcc/cmake/ninja/sqlite/pkg-config)
#   cmake -S database -B database/build -G Ninja
#   cmake --build database/build
# Produces: database/build/master_db_builder
