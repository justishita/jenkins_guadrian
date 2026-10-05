FROM jenkins/jenkins:lts-jdk17

USER root
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		python3 \
		python3-venv \
		python3-pip \
		git \
		curl \
		jq \
		docker-cli \
	&& rm -rf /var/lib/apt/lists/* \
	&& mkdir -p /var/jenkins_conf \
	&& chown jenkins:jenkins /var/jenkins_conf

USER jenkins
COPY --chown=jenkins:jenkins plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
COPY --chown=jenkins:jenkins jenkins.yaml /var/jenkins_conf/jenkins.yaml
COPY --chown=jenkins:jenkins jobs.groovy /var/jenkins_conf/jobs.groovy

ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
ENV CASC_JENKINS_CONFIG=/var/jenkins_conf/jenkins.yaml