# ps4tools

Python helpers around Il2CppDumper for Unity IL2CPP games on PS4 / PS5 with [ps4debug](https://github.com/GoldHEN/ps4debug)
(same workflow as the Unreal tools in [ps4-unreal-dumper](https://github.com/TetzkatLipHoka/ps4-unreal-dumper)).

Requirements: Python 3.10+, `numpy`, and for live access the `ps4debug` Python module. `ps4dump.py` from
ps4-unreal-dumper makes the memory dumps (`dump.bin` + `dump.json`).

| Tool | Does |
|---|---|
| `il2mem.py <dumpdir>` | pulls `global-metadata.dat` and a loadable image of `Il2CppUserAssemblies.prx` out of a memory dump, so Il2CppDumper runs without touching the PKG. Run the dumper on the two outputs with `"NoRedirectedPointer": true` in config.json and the printed base as dump address, put the result in `<dumpdir>/sdk`. |
| `il2live.py <dumpdir> "<path>" [--host IP] [--set V]` | reads / writes a value by a path that starts at a static field. Without `--host` it resolves against the dump (offline check), with `--host` live. `--find word` or `--find 50` lists paths from every static field (`--depth`). |
| `il2diff.py <dumpdir> a.json b.json --eq 50 77` | two dumps of one session, value before and after: every matching address, walked back to a static field and printed as an il2live path. |
| `il2diff.py <dumpdir> a.json b.json --changed [--class Name] [--same c.json]` | no value needed: every int / float / bool / enum field of a game object that differs between the dumps, then paths for the first hits. `--same` is a third dump of the unchanged state that filters moving values. |

Path syntax, passed through GUIs unchanged:

```
Class::field            static field of Class (root); namespace optional: Ns.Class::field, nested classes Outer.Inner
.field                  instance field, base class fields included
[i]                     element i of an array or List<T>
{42} {text}             Dictionary entry by int or string key
{Type=HealthCurrent}    Dictionary<System.Type, ...> entry by the type it stands for
{_guid=...} {name=...}  Dictionary entry by an identifier field of the key object
{#3}                    Dictionary entry by index (last resort)
:Class                  downcast (normally automatic: the object's real class is read from its klass pointer)
```

Example (Castlevania: Belmont's Curse, PS5):

```
python il2mem.py castle                                   # castle/ holds dump.bin + dump.json
Il2CppDumper castle/Il2CppUserAssemblies.prx.elf castle/global-metadata.dat castle/sdk
python il2diff.py castle dump_mana_50.json dump_mana_77.json --eq 50 77
python il2live.py castle "GameStateManager::_instance._mainCharacterEntity.componentCache{609408304}.CurrentStats{Type=HealthCurrent}.baseValue" --host 192.168.0.10 --set 999
```

Notes measured on real dumps (metadata v29 / v31, x64):

* object header 0x10 (`klass*`, `monitor*`); dump.cs class field offsets include it, struct offsets do not
* `Il2CppClass`: name 0x10, namespace 0x18, declaringType 0x50, parent 0x58, static_fields 0xB8
* arrays: elements from 0x20; `List<T>`: `_items` 0x10, `_size` 0x18; `Dictionary<K,V>`: `_entries` 0x18, `_count` 0x20, entry = {int hash, int next, key @8, value @0x10}
* static roots and classes are resolved in the running process through the il2cpp TypeInfo table, so the dump may come from an earlier session of the same build; instance addresses in `--eq`/`--changed` output are only valid for the session the dumps came from
* the first run on a dump builds `<dump>.bin.klasses.json` (about a minute), later runs take seconds
* values shown on screen are not always the stored ones (a marker counter showed 5 while the list had 11 entries): when `--eq` finds nothing, use `--find` by name or `--changed`
