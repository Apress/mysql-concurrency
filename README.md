# Apress Source Code

This repository accompanies [*MySQL Concurrency*](https://www.apress.com/9781484266519) by Jesper Wisborg Krogh (Apress, 2021).

[comment]: #cover
![Cover image](9781484266519.jpg)

Download the files as a zip using the green button, or clone the repository to your machine using Git.

## Releases

Release v1.0 corresponds to the code in the published book, without corrections or updates.

Release v1.1 fixes the directory structure, adds missing files for chapters 2, 13, 15, 16, 17, 18, and adds error message when using `concurrency_book.generate.load()` with the classic MySQL protocol.

## Docker 

This is optional but for the sake of studying it's better to use Docker instead 

Start the container 
```
$ docker-compose up -d
```

copy the whole project to `./docker/db/mysql/.mysqlsh` directory with the name as `mysql-concurrency`.

add `mysqlshrc.py` file inside `./docker/db/mysql/.mysqlsh` and copy this code 
```mysqlshrc.py
import sys
sys.path.append('/root/.mysqlsh/mysql-concurrency')
import concurrency_book.generate
```

Enter to MySQL Shell
```
$ docker-compose exec mysql mysqlsh -uroot
```

## Contributions

See the file Contributing.md for more information on how you can contribute to this repository.
