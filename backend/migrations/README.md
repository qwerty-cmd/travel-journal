# Migrations

SQL migrations for the Postgres schema (Trip, Stop, Photo, Bike — spec Section 3).
Written in Session 1 alongside the Pydantic models in `app/models/`, once the data
model is locked. Run against both the local docker-compose Postgres and Neon
using the same `DATABASE_URL`-driven tooling.
