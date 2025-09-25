;*********************************************************
;	SAMPLE USER AREA
;	FOR THINKER TOYS 2D CONTROLLER AND SWITCHBOARD
;************************************************************
;
;       THIS DRIVER IMPLEMENTS I/O BYTE
;THE CONSOLE DEVICE IS AN RS232 OR TTY TERMINAL
;ATTACHED TO THE I/O CONNECTOR OF THE DJ2D BOARD
;THE LIST DEVICE IS AN RS232 OR TTY PRINTER
;ATTACHED TO THE SECOND SERIAL PORT ON THE SWITCHBOARD
;	THESE ROUTINES CAN BE USED AS A BASIS
;FOR THE DEVELOPMENT OF YOUR OWN I/O
;	NOTE ALSO LOCATIONS OF WHERE TO SET THE
;DENSITY ON EACH DRIVE AND SETTING OF THE MODE BYTE
;WHICH CAN RUN A FILE NAMED "AUTO.COM" ON EITHER
;COLD OR WARM BOOT.
;	FOR EXAMPLE, RENAME BASIC.COM TO AUTO.COM
;USING THE COMMAND "REN AUTO.COM=BASIC.COM" AND
;HAVE BASIC AUTOMATICALLY ACTIVATED ON EACH COLD BOOT.
;
;*********************************************************
;	SYSTEM EQUATES
;*********************************************************
;
MSIZE	EQU	48		;CP/M SYSTEM SIZE IN KBYTES
BIAS	EQU	(MSIZE-16)*1024
CPMB	EQU	2600H+BIAS	;LOCATION OF CCP
BDOS	EQU	2E00H+BIAS	;LOCATION OF BDOS
BIOS	EQU	3B00H+BIAS	;LOCATION OF BIOS
USER	EQU	BIOS+380H	;START OF USER AREA
OFFSET	EQU	1E80H-BIOS	;TO SYSGEN IMAGE
;
;*********************************************************
;	DISK PARAMETERS - DOUBLE DENSITY DISK JOCKEY
;*********************************************************
;	ON DISK			IN SYSGEN	IN 24K SYSTEM
;	TRACK	SECTOR		ADDRESS
;BOOT	0	1		 900H		0E700H
;CCP	1	1		0980H		4600H
;BDOS	1	17		1180H		4E00H
;BIOS	1	43		1E80H		5B00H
;MODE	1	49		21FFH		5E7FH
;USER	1	50-52		2200H		5E80H
;TOP OF SYSTEM			237FH		5FFFH
;
;DOUBLE DENSITY SKIP TABLE
;	1,2,19,20,37,38,3,4,21,22,39,40,5,6,23,24,41,42
;	7,8,25,26,43,44,9,10,27,28,45,46,11,12,29,30,47,48
;	13,14,31,32,49,50,15,16,33,34,51,52,17,18,35,36
;
;*********************************************************
;MODE BYTE OPTIONS AND DENSITY SETTINGS OF VARIOUS DRIVES
;*********************************************************
;
	ORG	USER-8		;5E78H IN 24K SYSTEM
DNSTY:	DB	1,0,1,1		;DRIVE B SD, OTHERS DD
		;B0=DENSITY	0=SNGL,	1=DBL
	DB	0,0,0		;RESERVED
MODE:	DB	0		;MODE BYTE
		;BIT0=1 DOES AUTO ON COLD BOOT
		;BIT1=1 DOES AUTO ON WARM BOOT
*********************************************************
*	SAMPLE USER AREA
*********************************************************
	ORG	USER		;5E80H IN DIST SYSTEM

*******************************************************
* JUMP TABLE - JMPS MUST REMAIN HERE, IN SAME ORDER
*******************************************************

	JMP	INIT		;INITIALIZATION
	JMP	CONST		;CONSOLE STATUS
	JMP	CONIN		;CONSOLE INPUT
	JMP	CONOUT		;CONSOLE OUTPUT
	JMP	LIST		;LIST OUTPUT
	JMP	PUNCH		;PUNCH OUTPUT
	JMP	READER	;READER INPUT
	JMP	PRST		;PRINTER STATUS

CDISK	EQU	4	;current disk storage location
IOBYTE	EQU	3H	;iobyte storage location

************************************************************
*                                                          *
* Iobyte allows selection of different I/O devices. It     *
* can be initialized in any way by changing the equate     *
* bellow.                                                  *
* Initial iobyte is currently defined as :                 *
* console = tty                                            *
* reader = tty                                             *
* punch = tty                                              *
* list = tty                                               *
*                                                          *
************************************************************

INTIOBY	EQU	0	;initial iobyte,

************************************************************
*                                                          *
* The following equates reference the disk jockey/2d       *
* controller board. If your controller is non-standard     *
* then all the equates can be changed by re-assigning the  *
* value of ORIGIN to be the starting address of your       *
* controller.                                              *
*                                                          *
************************************************************

ORIGIN	EQU	0E000H	;disk jockey/2d beginning address
INPUT	EQU	ORIGIN+3	;serial input routine
OUTPUT	EQU	ORIGIN+6	;serial output routine
TSTAT	EQU	ORIGIN+21H	;serial device status routine
ACR	EQU	0DH	;carriage return
ALF	EQU	0AH	;line feed

************************************************************
*                                                          *
* const: get the status for the currently assigned console *
*        device. The console device can be gotten from     *
*        iobyte, then a jump to the correct console status *
*        routine is performed.                             *
*                                                          *
************************************************************

CONST	LXI	H,CSTBLE	;beginning of jump table
	JMP	CONIN1	;select corre jump

************************************************************
*                                                          *
* csreader: if the console is assigned to the reader then  *
*           a jump will be made here, where another jump   *
*           will occur to the correct reader status.       *
*                                                          *
************************************************************

CSREADR	LXI	H,CSRTBLE	;beginning of reader status table
	JMP	READERA

************************************************************
*                                                          *
* conin: take the correct jump for the console input       *
*        routine. The jump is based on the two least sig-  *
*        nificant bits of iobyte.                          *
*                                                          *
************************************************************

CONIN	LXI	H,CITBLE	;beginning of character input table

*
* entry at conin1 will decode the two least significant bits
* of iobyte. This is used by conin,conout, and const.
*

CONIN1	LDA	IOBYTE
	RAL

*
* entry at seldev will form an offset into the table pointed
* to by H&L and then pick up the address and jump there.
*

SELDEV	ANI	6H	;strip off unwanted bits
	MVI	D,0	;form affset
	MOV	E,A
	DAD	D	;add offset
	MOV	A,M	;pick up high byte
	INX	H
	MOV	H,M	;pick up low byte
	MOV	L,A	;form address
	PCHL		;go there !

************************************************************
*                                                          *
* conout: take the proper branch address based on the two  *
*         least significant bits of iobyte.                *
*                                                          *
************************************************************

CONOUT	LXI	H,COTBLE	;beginning of the character out table
	JMP	CONIN1	;do the decode

************************************************************
*                                                          *
* reader: select the correct reader device for input. The  *
*         reader is selected from bits 2 and 3 of iobyte.  *
*                                                          *
************************************************************

READER	LXI	H,RTBLE	;beginning of reader input table

*
* entry at readera will decode bits 2 & 3 of iobyte, used
* by csreader.
*

READERA	LDA	IOBYTE

*
* entry at reader1 will shift the bits into position, used
* by list and punch.
*

READR1	RAR
	JMP	SELDEV

************************************************************
*                                                          *
* punch: select the correct punch device. The seection     *
*        comes from bits 4&5 of iobyte.                    *
*                                                          *
************************************************************

PUNCH	LXI	H,PTBLE	;beginning of punch table
	LDA	IOBYTE

*
* entry at pnch1 rotates bits a little more in prep for
* seldev, used by list.
*

PNCH1	RAR
	RAR
	JMP	READR1

************************************************************
*                                                          *
* list: select a list device based on bits 6&7 of iobyte   *
*                                                          *
************************************************************

LIST	LXI	H,LTBLE	;beginning of the list device routines
	LDA	IOBYTE
	RAR
	RAR
	JMP	PNCH1

************************************************************
*                                                          *
* If customizing I/O routines is being performed, the      *
* table below should be modified to reflect the changes.   *
* all I/O devices are decoded out of iobyte and the jump   *
* is taken from the following tables.                      *
*                                                          *
************************************************************



*
* console input table
*

CITBLE	DW	CITTY	;input from tty (currently assigned by intioby,input from 2d)
	DW	CICRT	;input from crt (currently SWITCHBOARD serial port 1)
	DW	READER	;input from reader (depends on reader selection)
	DW	CIUC1	;input from user console 1 (currently SWITCHBOARD serial port 1)

*
* console output table
*

COTBLE	DW	COTTY	;output to tty (currently assigned by intioby,output to 2d)
	DW	COCRT	;output to crt (currently SWITCHBOARD serial port 1)
	DW	LIST	;output to list device (depends on bits 6&7 of iobyte)
	DW	COUC1	;output to user console 1 (currently SWITCHBOARD serial port 1)

*
* list device table
*

LTBLE	DW	COTTY	;output to tty (currently assigned by intioby,output to 2d)
	DW	COCRT	;output to crt (currently SWITCHBOARD serial port 1)
	DW	COLPT	;output to line printer (currently SWITCHBOARD serial port 1)
	DW	COUL1	;output to user line printer 1 (currently SWITCHBOARD serial port 1)

*
* punch device table
*

PTBLE	DW	COTTY	;output to the tty (currently assigned by intioby,output to 2d)
	DW	COPTP	;output to paper tape punch (currently SWITCHBOARD serial port 1)
	DW	COUP1	;output to user punch 1 (currently SWITCHBOARD serial port 1)
	DW	COUP2	;output to user punch 2 (currntlly SWITCHBOARD serial port 1)

*
* reader device input table
*

RTBLE	DW	CITTY	;input from tty (currently assigned by intioby, input from 2d)
	DW	CIPTR	;input from paper tape reader (currently SWITCHBOARD serial port 1)
	DW	CIUR1	;input from user reader 1 (currently SWITCHBOARD serial port 1)
	DW	CIUR2	;input from user reader 2 (currently SWITCHBOARD serial port 1)

*
* console status table
*

CSTBLE	DW	CSTTY	;status of tty (currently assigned by intioby, ststus from 2d)
	DW	CSCRT	;status from crt (currently SWITCHBOARD serial port 1)
	DW	CSREADR	;status from reader (depends on reader device )
	DW	CSUC1	;status from user console 1 (currently SWITCHBOARD serial port 1)

*
* status from reader device
*

CSRTBLE	DW	CSTTY	;status from tty (currently assigned by intioby, status of 2d)
	DW	CSPTR	;status from paper tape reader (currently SWITCHBOARD serial port 1)
	DW	CSUR1	;status from user reader 1 (currently SWITCHBOARD serial port 1)
	DW	CSUR2	;status of user reader 2 (currently SWITCHBOARD serial port 1)

************************************************************
*                                                          *
* The following equates set output device to output to     *
* the SWITCHBOARD serial port 1.                           *
*                                                          *
************************************************************

COCRT	EQU	$	;output from crt
COUC1	EQU	$	;output from user console 1
COUL1	EQU	$	;output from user line printer 1
COPTP	EQU	$	;output from paper tape punch
COUP1	EQU	$	;output from user punch 1
COUP2	EQU	$	;output from user punch 2
COLPT	IN	0	;output from line printer,get status
	ANI	2H	;wait until ok to send
	JZ	COLPT
	MOV	A,C	;output the character
	OUT	0
	RET

************************************************************
*                                                          *
* CALL ZAPPLE FOR CONOUT				   *
*                                                          *
************************************************************

COTTY	CALL	0F009H
	RET

************************************************************
*                                                          *
* The following equates set the input from the devices to  *
* come from the SWITCHBOARD serial port 1                  *
*                                                          *
************************************************************

CIUC1	EQU	$	;input from user console 1
CICRT	EQU	$	;input from crt
CIUR1	EQU	$	;input from user reader 1
CIUR2	EQU	$	;input from user reader 2
CIPTR	IN	2	;input from paper tape reader, get status
	ANI	40H	;wait for character
	JZ	CIPTR
	IN	1
	ANI	7FH	;strip off the parity
	RET

************************************************************
*                                                          *
* CALL ZAPPLE C_FOR CONIN				     *
*                                                          *
************************************************************

CITTY	CALL	0F003H
	RET

************************************************************
*                                                          *
* CALL ZAPPLE FOR CONST					   *
*                                                          *
************************************************************

CSTTY	CALL	0F012H
	RET

************************************************************
*                                                          *
* The following equates cause the devices to get status    *
* from the SWITCHBOARD serial port 1.                      *
*                                                          *
************************************************************

PRST	EQU	$	;STATUS OF PRINTER
CSUR1	EQU	$	;status of user reader 1
CSUR2	EQU	$	;status of user reader 2
CSPTR	EQU	$	;status of paper tape reader
CSUC1	EQU	$	;status of user console 1
CSCRT	CALL	0F012H
	RET

***********************************************************
*                                                         *
* THE FOLLOWING IS A TERMINAL INITIALIZATION ROUTINE.     *
* IT CAN BE USED TO PERFORM ANY INITIALIZATION YOU MAY    *
* REQUIRE. CURRENTLY IT IS NOT NEEDED.                    *
*                                                         *
***********************************************************

INIT	EQU	$	;
	MVI	A,INTIOBY
	STA	IOBYTE
	RET
