# The image for the "ledger" profile.
#
# Build it, then point the profile's `image` at the tag:
#   docker build -f ledger.Dockerfile -t orchestration/ledger:latest .
#
# Starting from the base agent image means the toolchain, the narration
# helper, the browser and the agent prompt are already there -- add only
# what this particular agent needs. That is what keeps a job a container
# start rather than an install.
FROM orchestration/claude-code:latest

USER root

# Whatever this agent's work needs and the base image lacks.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#         postgresql-client \
#     && rm -rf /var/lib/apt/lists/*

# RUN pip install --no-cache-dir sqlalchemy alembic

USER agent
