-- 01-roles.sh가 psql 변수(owner_pw, app_pw)와 함께 실행한다. (.tpl이라 initdb가 직접 실행하지 않는다)
CREATE ROLE nw_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE nw_app LOGIN PASSWORD :'app_pw';

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO nw_owner;
GRANT USAGE ON SCHEMA public TO nw_app;

-- nw_owner가 만드는 테이블·시퀀스에 nw_app의 DML 권한을 자동 부여
ALTER DEFAULT PRIVILEGES FOR ROLE nw_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nw_app;
ALTER DEFAULT PRIVILEGES FOR ROLE nw_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO nw_app;
