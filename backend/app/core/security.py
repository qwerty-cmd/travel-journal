# Slug-based access control (spec Section 4 "Access control").
# No passwords, no accounts — a request's rider/viewer slug is looked up
# against Trip in the data/ layer, and write endpoints reject a viewer slug.
#
# Filled in during Session 1 (API contract) alongside the Trip repository
# in data/ — needs the Trip schema locked first.
