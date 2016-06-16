test:
	sudo python -m unittest discover -s tests -v -p '*test*.py' > tmp

patternbench:
	sudo python -m benchmarks.patternbench -t "pattern_on_fs()"

fiobench:
	sudo python -m benchmarks.fiobench -t 'compare_fs()'

appbench:
	sudo python -m benchmarks.appbench -t 'bench()'

leveldb:
	sudo python -m benchmarks.appbench -t 'leveldbbench()'
f2fsgc:
	mkdir -p bin
	cd ./foreign && gcc -o forcef2fsgc forcef2fsgc.c && mv forcef2fsgc ../bin/

