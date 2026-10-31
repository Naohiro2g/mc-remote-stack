# Paper用Temurin runtime image

Stackの公開VPS用Minecraft runtimeは、itzg Minecraft Server imageのdigest固定版にTemurin JREを上書きした派生imageです。

- Java: Temurin 25.0.5+8
- Container environment: `JAVA_VERSION=jdk-25.0.5+8`
- JRE archive: `OpenJDK25U-jre_x64_linux_hotspot_25.0.5_8.tar.gz`
- JRE SHA-256: `6fac98f0c8202efc3135a9e9e42aa61963ec42d1c5cbb40caf231e7b14a24ef6`
- Base image: `docker.io/itzg/minecraft-server:2026.7.2-java25@sha256:6ec1110e4d9236d00ae9436a3e4a5929583e5b19cc94b756a7c603f7cf647a77`

[`publish-temurin-paper-runtime.yml`](../.github/workflows/publish-temurin-paper-runtime.yml) は、手動実行時に上記URLからJREを取得し、SHA-256を照合してimageをbuildします。workflowとDockerfileのAction参照はfull commit SHAで固定し、`GITHUB_TOKEN`には`packages: write`だけを付与します。

公開先は`ghcr.io/naohiro2g/mc-remote-paper-runtime-temurin:temurin-25.0.5-8`です。workflowはpush後に`JAVA_VERSION`と`java -version`を照合します。imageを更新するときは新しいpatch版・archive SHA・base image digestを同時に改訂し、workflow実行結果のregistry manifest digestをimmutable presetへ記録します。タグ名やDocker config IDは、registry manifest digestの代わりに使用しません。
