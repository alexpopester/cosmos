#!/nix/store/gik3rh1vz2jlgnifb9dh6vc6sxwwz9jj-bash-5.3p9/bin/sh

docker volume create clamav
docker run -it --rm -v clamav:/var/lib/clamav clamav/clamav freshclam
