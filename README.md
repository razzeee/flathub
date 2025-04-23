# Filezilla Flatpak

## Initializing

```
git submodule update --init
```

## Building

```
flatpak run org.flatpak.Builder build-dir --user --ccache --force-clean --install org.filezilla-project.FileZilla.json
```

Then you can run it via the command line:

```
flatpak run org.filezilla-project.FileZilla
```

or just search for the installed app on your system
