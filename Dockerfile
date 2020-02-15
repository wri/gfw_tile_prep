FROM osgeo/gdal:ubuntu-small-3.0.4

ENV DIR=/usr/local/app

#RUN sed -i '/^[fedora]/a\exclude=postgresql*' /etc/yum.repos.d/fedora.repo \
#    && sed -i '/^[updates]/a\exclude=postgresql*' /etc/yum.repos.d/fedora-updates.repo
#
#RUN dnf install -y https://download.postgresql.org/pub/repos/yum/12/fedora/fedora-30-x86_64/pgdg-fedora-repo-latest.noarch.rpm

#RUN apt-get install -y \
#    make \
#    automake \
#    gcc \
#    gcc-c++ \
#    kernel-dev \
##    libpq-dev \
##    python3 \
##    python3-dev \
##    gdal \
##    gdal-python-tools \
#    && dnf clean all
RUN apt update -y && apt install -y python3-pip libpq-dev ca-certificates
RUN update-ca-certificates
RUN mkdir -p /etc/pki/tls/certs
RUN cp /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt

RUN mkdir -p ${DIR}
WORKDIR ${DIR}

COPY requirements.txt .
RUN pip3 install -r requirements.txt

COPY . .
RUN pip3 install -e .

# Set current work directory to /tmp. This is important when running as AWS Batch job
# When using the ephemeral-storage launch template /tmp will be the mounting point for the external storage
# In AWS batch we will then mount host's /tmp directory as docker volume /tmp
WORKDIR /tmp

ENTRYPOINT ["pixetl"]