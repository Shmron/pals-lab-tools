# HarmonAIze

**Live:** <https://portal.placealert.org/harmonaize/>
**Code:** <https://github.com/Shmron/HarmonAIze> (PALs Lab fork)
**Upstream project:** <https://github.com/drnicholasbrink/HarmonAIze>

HarmonAIze is an open-source toolkit that streamlines harmonising and integrating climate and health data. It is designed for research teams in resource-constrained settings and uses AI-assisted workflows to map, clean and unify heterogeneous datasets, with a privacy-preserving, federated approach so sensitive health data does not have to leave its owner. Within the PALs Portal it is presented as AI-assisted geolocation validation: turning messy facility and address records into research-ready locations that can be linked to environmental data.

## Where the code lives

This folder deliberately holds **no source code**. HarmonAIze keeps its own repository and history:

| Repository | Role |
|---|---|
| [`drnicholasbrink/HarmonAIze`](https://github.com/drnicholasbrink/HarmonAIze) | The upstream open-source project. Please credit it when you use or cite HarmonAIze. |
| [`Shmron/HarmonAIze`](https://github.com/Shmron/HarmonAIze) | The PALs Lab fork, which is the version deployed on the PALs Portal. |

Keeping it separate avoids duplicating the project's history and lets the fork stay in step with upstream.

## Stack

Django (built from the Cookiecutter Django template), PostgreSQL, Celery workers, and Docker Compose. The code is organised into apps for core logic, health data, climate data and geolocation. It is released under the MIT licence.

## Running it

Everything runs in Docker, so no local Python packages are needed. In outline (see the fork's README for the full steps):

1. Install Docker Desktop and clone the fork.
2. Start it with `docker-compose -f docker-compose.local.yml up -d`, then run the database migrations and create a superuser.
3. Open <http://localhost:8000>.

The transformation-suggestion features call the OpenAI API. Set your own `OPENAI_API_KEY` in the container environment file the fork describes. **Never commit a real key.**

## How it fits with the other tools

The Africa road maps in this repository reuse the Africa administrative boundaries file that ships with HarmonAIze's geolocation module (see [`../africa-road-density`](../africa-road-density) and [`../africa-road-proximity`](../africa-road-proximity)). Cleaned and validated locations from HarmonAIze are the kind of input that [PALs Earth](../palsearth) needs to extract exposures reliably.

## Notes

- Evaluation of geocoding performance is still in progress. Please do not quote accuracy figures for HarmonAIze until a documented evaluation is published.

Part of [PALs Lab Tools](../../README.md). The code in the linked repositories is released under the MIT licence.
