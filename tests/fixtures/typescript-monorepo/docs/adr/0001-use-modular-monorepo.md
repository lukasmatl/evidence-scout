# ADR 0001: Use a modular TypeScript monorepo

Date: 2026-06-20

## Status

Accepted

## Context

The sample workspace contains a NestJS API, a Next.js web app, and shared
OpenAPI contracts.

## Decision

Use a modular TypeScript monorepo so applications and contracts can be scanned
as one workspace while retaining package boundaries.

## Consequences

The fixture exercises package discovery, OpenAPI extraction, ADR extraction,
and cross-package repository metadata in one representative workspace.
