create table users 
(id serial primary key,
telegram_id integer not null unique,
name varchar (20) not null,
limit_balance numeric not null,
persent_balance integer,
alert_balance boolean default true);

create type transaction_side as enum ('spend', 'income');

create table category
(id serial primary key,
name varchar (20) not null,
side transaction_side not null,
description text,
persent_category integer,
limit_category numeric,
alert boolean default true);

create table transaction 
(id serial primary key,
user_id integer not null,
date date not null,
description text,
sum numeric not null,
side transaction_side not null,
category_id integer not null,
foreign key (user_id) references users(id),
foreign key (category_id) references category(id));

